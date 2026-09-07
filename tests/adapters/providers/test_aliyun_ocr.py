"""百炼 OCR 的真实协议形状、媒体边界和来源计量离线回归。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

from rag_app.adapters.providers.aliyun_ocr import (
    AliyunOcrAdapter,
    AliyunOcrConfig,
    inspect_ocr_image,
    ocr_input_token_estimate,
)
from rag_app.adapters.providers.budget_transport import BudgetedTransport
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.core.errors import (
    PolicyDenied,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
)


def _image(*, size: tuple[int, int] = (320, 160), fmt: str = "PNG") -> bytes:
    image = Image.new("RGB", size, color="white")
    stream = io.BytesIO()
    image.save(stream, format=fmt)
    return stream.getvalue()


def _adapter(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: AliyunOcrConfig | None = None,
) -> AliyunOcrAdapter:
    return AliyunOcrAdapter(
        config or AliyunOcrConfig(egress_allowed=True),
        http_client=ProviderHttpClient(
            "https://dashscope.aliyuncs.com",
            client=httpx.Client(
                transport=BudgetedTransport(
                    httpx.MockTransport(handler),
                    ledger_path=tmp_path / "budget.sqlite3",
                )
            ),
            max_attempts=2,
            defer_success_observation=True,
            sleeper=lambda _: None,
        ),
        api_key_resolver=lambda: "public-synthetic-credential",
    )


def _response(
    *, finish: str = "stop", usage: object = None
) -> dict[str, object]:
    return {
        "model": "qwen3.5-ocr",
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": "合成图片编号314",
                },
            }
        ],
        "usage": usage,
    }


@pytest.mark.parametrize(
    ("fmt", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg")]
)
def test_multimodal_protocol_preserves_original_image_and_unknown_confidence(
    tmp_path: Path,
    fmt: str,
    mime: str,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_response(
                usage={
                    "prompt_tokens": 222,
                    "completion_tokens": 8,
                    "total_tokens": 230,
                    "prompt_tokens_details": {"image_tokens": 100},
                }
            ),
        )

    data = _image(fmt=fmt)
    digest = hashlib.sha256(data).hexdigest()
    adapter = _adapter(tmp_path, handler)
    try:
        result = adapter.recognize(data, media_type=mime, media_sha256=digest)
        assert result.text == "合成图片编号314"
        assert result.media_sha256 == digest and result.origin == "ocr"
        assert result.confidence is None and result.bbox is None
        assert result.width == 320 and result.height == 160
        assert result.usage.image_tokens == 100
        assert result.call.observed_tokens == 230
        assert result.call.estimated_tokens == ocr_input_token_estimate()
        assert result.call.operation == "image.ocr"
        assert result.complete
        request = requests[0]
        payload = json.loads(request.content)
        image = payload["messages"][0]["content"][0]
        assert image["type"] == "image_url"
        assert image["max_pixels"] == 1_048_576
        assert image["min_pixels"] == 3072
        assert set(image["image_url"]) == {"url"}
        uri = image["image_url"]["url"]
        assert uri.startswith(f"data:{mime};base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == data
        assert "enable_thinking" not in payload
        assert "response_format" not in payload
        assert payload["max_tokens"] == 4096
        assert request.extensions["rag_chat_operation"] == "image.ocr"
        assert "base64" not in result.call.model_dump_json()
        assert "合成图片编号" not in repr(result)
    finally:
        adapter.close()


@pytest.mark.parametrize("reason", ["emf", "mismatch", "hash", "corrupt"])
def test_bad_media_never_reaches_http(tmp_path: Path, reason: str):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_response())

    data = b"invalid-png" if reason == "corrupt" else _image()
    digest = "0" * 64 if reason == "hash" else hashlib.sha256(data).hexdigest()
    mime = {"emf": "image/emf", "mismatch": "image/jpeg"}.get(
        reason, "image/png"
    )
    adapter = _adapter(tmp_path, handler)
    try:
        with pytest.raises(ValueError, match="OCR_"):
            adapter.recognize(data, media_type=mime, media_sha256=digest)
        assert not calls
    finally:
        adapter.close()


def test_pixel_limit_is_checked_before_model_call():
    data = _image(size=(1500, 1500))
    with pytest.raises(ProviderInputTooLarge, match="OCR_IMAGE_PIXEL_LIMIT"):
        inspect_ocr_image(
            data,
            media_type="image/png",
            media_sha256=hashlib.sha256(data).hexdigest(),
            config=AliyunOcrConfig(),
        )


def test_truncated_ocr_remains_error_with_known_usage(tmp_path: Path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=_response(
                finish="length",
                usage={"total_tokens": 31},
            ),
        )

    data = _image()
    adapter = _adapter(tmp_path, handler)
    try:
        with pytest.raises(ProviderInvalidResponse) as raised:
            adapter.recognize(
                data,
                media_type="image/png",
                media_sha256=hashlib.sha256(data).hexdigest(),
            )
        assert len(calls) == 1
        assert raised.value.provider_call is not None
        assert raised.value.provider_call.observed_tokens == 31
        assert "CHAT_OUTPUT_TRUNCATED" in str(raised.value.details)
    finally:
        adapter.close()


def test_ocr_without_egress_authorization_is_blocked(tmp_path: Path):
    adapter = _adapter(
        tmp_path,
        lambda _: pytest.fail("禁止未授权 OCR 请求"),
        config=AliyunOcrConfig(),
    )
    data = _image()
    try:
        with pytest.raises(PolicyDenied, match="OCR_EGRESS_NOT_AUTHORIZED"):
            adapter.recognize(
                data,
                media_type="image/png",
                media_sha256=hashlib.sha256(data).hexdigest(),
            )
    finally:
        adapter.close()


def test_transient_ocr_retry_is_bounded_and_usage_unknown(tmp_path: Path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return (
            httpx.Response(503, json={})
            if len(calls) == 1
            else httpx.Response(200, json=_response())
        )

    data = _image()
    adapter = _adapter(tmp_path, handler)
    try:
        result = adapter.recognize(
            data,
            media_type="image/png",
            media_sha256=hashlib.sha256(data).hexdigest(),
        )
        assert len(calls) == 2
        assert result.call.retry_count == 1 and result.call.attempt_count == 2
        assert result.call.observed_tokens is None
        assert result.usage.total_tokens is None
    finally:
        adapter.close()
