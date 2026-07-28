from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rag_app.clients.resilience import (
    ExternalServiceUnavailableError,
    ResiliencePolicy,
    ResilientHttpPool,
)
from rag_app.ocr.client import OcrClient
from rag_app.ocr.models import (
    DEFAULT_OCR_REVISION,
    OcrEngineResult,
    OcrLine,
)
from rag_app.ocr.paddle_engine import PaddleOcrEngine
from rag_app.ocr.rasterize import (
    RasterizationError,
    SandboxedEmfRasterizer,
)
from rag_app.ocr.service import OcrLimits, create_ocr_app

_REVISION = DEFAULT_OCR_REVISION


class _Engine:
    def ready(self) -> bool:
        return True

    def recognize(self, image_bytes: bytes) -> OcrEngineResult:
        assert image_bytes.startswith(b"\x89PNG")
        return OcrEngineResult(
            lines=(
                OcrLine(
                    text="合成图文字",
                    confidence=0.98,
                    bbox=(1, 1, 8, 8),
                ),
            )
        )


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(stream, format="PNG")
    return stream.getvalue()


def _ocr_payload(
    *,
    media_sha256: str,
    revision: str = _REVISION,
) -> dict[str, object]:
    return {
        "media_sha256": media_sha256,
        "ocr_revision": revision,
        "text": "合成图文字",
        "confidence": 0.98,
        "lines": [
            {
                "text": "合成图文字",
                "confidence": 0.98,
                "bbox": [1, 1, 8, 8],
            }
        ],
        "width": 10,
        "height": 10,
        "elapsed_ms": 4,
    }


def test_ocr_api_checks_hash_revision_and_pixels() -> None:
    image = _png()
    app = create_ocr_app(
        engine=_Engine(),
        revision=_REVISION,
        limits=OcrLimits(
            max_input_bytes=1024,
            max_pixels=100,
            request_timeout_seconds=1.0,
            max_concurrency=1,
        ),
    )
    client = TestClient(app)
    payload = {
        "media_sha256": hashlib.sha256(image).hexdigest(),
        "ocr_revision": _REVISION,
        "media_type": "image/png",
        "content_base64": base64.b64encode(image).decode("ascii"),
    }

    response = client.post("/v1/ocr", json=payload)
    wrong_hash = client.post(
        "/v1/ocr",
        json={**payload, "media_sha256": "0" * 64},
    )
    wrong_revision = client.post(
        "/v1/ocr",
        json={**payload, "ocr_revision": "wrong"},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "合成图文字"
    assert response.json()["confidence"] == 0.98
    assert wrong_hash.status_code == 422
    assert wrong_revision.status_code == 409


def test_ocr_api_rejects_wrong_token_and_declared_type() -> None:
    image = _png()
    app = create_ocr_app(
        engine=_Engine(),
        revision=_REVISION,
        limits=OcrLimits(1024, 100, 1.0, 1),
        api_token="a" * 32,
    )
    client = TestClient(app)
    payload = {
        "media_sha256": hashlib.sha256(image).hexdigest(),
        "ocr_revision": _REVISION,
        "media_type": "image/jpeg",
        "content_base64": base64.b64encode(image).decode("ascii"),
    }

    unauthorized = client.post("/v1/ocr", json=payload)
    mismatch = client.post(
        "/v1/ocr",
        json=payload,
        headers={"Authorization": f"Bearer {'a' * 32}"},
    )

    assert unauthorized.status_code == 401
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"] == "IMAGE_TYPE_MISMATCH"


def test_ocr_client_preserves_media_revision_cache_seam() -> None:
    image = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode("utf-8")
        assert hashlib.sha256(image).hexdigest() in payload
        return httpx.Response(
            200,
            json={
                "media_sha256": hashlib.sha256(image).hexdigest(),
                "ocr_revision": _REVISION,
                "text": "合成图文字",
                "confidence": 0.98,
                "lines": [
                    {
                        "text": "合成图文字",
                        "confidence": 0.98,
                        "bbox": [1, 1, 8, 8],
                    }
                ],
                "width": 10,
                "height": 10,
                "elapsed_ms": 4,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    pool = ResilientHttpPool(
        ("http://ocr.example.invalid",),
        client=http_client,
        policy=ResiliencePolicy(1, 1, 1.0, 1),
    )
    client = OcrClient(
        pool,
        revision=_REVISION,
        api_token=None,
        max_input_bytes=1024,
    )

    result = client.recognize(
        image,
        media_type="image/png",
        media_sha256=hashlib.sha256(image).hexdigest(),
    )

    assert result.ocr_revision == _REVISION
    assert result.media_sha256 == hashlib.sha256(image).hexdigest()
    assert result.text == "合成图文字"


def test_ocr_revision_error_fails_over_by_endpoint() -> None:
    image = _png()
    media_sha256 = hashlib.sha256(image).hexdigest()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        return httpx.Response(
            200,
            json=_ocr_payload(
                media_sha256=media_sha256,
                revision="wrong" if host == "bad" else _REVISION,
            ),
        )

    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(2, 1, 30.0, 1),
    )
    client = OcrClient(
        pool,
        revision=_REVISION,
        api_token=None,
        max_input_bytes=1024,
    )

    result = client.recognize(
        image,
        media_type="image/png",
        media_sha256=media_sha256,
    )

    assert result.ocr_revision == _REVISION
    assert calls == ["bad", "good"]


def test_ocr_media_sha_error_fails_over_by_endpoint() -> None:
    image = _png()
    media_sha256 = hashlib.sha256(image).hexdigest()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        response_sha256 = "0" * 64 if host == "bad" else media_sha256
        return httpx.Response(
            200,
            json=_ocr_payload(media_sha256=response_sha256),
        )

    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(2, 1, 30.0, 1),
    )
    client = OcrClient(
        pool,
        revision=_REVISION,
        api_token=None,
        max_input_bytes=1024,
    )

    result = client.recognize(
        image,
        media_type="image/png",
        media_sha256=media_sha256,
    )

    assert result.media_sha256 == media_sha256
    assert calls == ["bad", "good"]


def test_ocr_invalid_schema_fails_over_by_endpoint() -> None:
    image = _png()
    media_sha256 = hashlib.sha256(image).hexdigest()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        payload = _ocr_payload(media_sha256=media_sha256)
        if host == "bad":
            payload.pop("width")
        return httpx.Response(200, json=payload)

    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(2, 1, 30.0, 1),
    )
    client = OcrClient(
        pool,
        revision=_REVISION,
        api_token=None,
        max_input_bytes=1024,
    )

    result = client.recognize(
        image,
        media_type="image/png",
        media_sha256=media_sha256,
    )

    assert result.width == 10
    assert calls == ["bad", "good"]


@pytest.mark.parametrize("invalid_field", ("confidence", "bbox"))
def test_ocr_nonfinite_values_fail_over_by_endpoint(
    invalid_field: str,
) -> None:
    image = _png()
    media_sha256 = hashlib.sha256(image).hexdigest()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        payload = _ocr_payload(media_sha256=media_sha256)
        if host == "bad" and invalid_field == "confidence":
            payload["confidence"] = "NaN"
        elif host == "bad":
            payload["lines"] = [
                {
                    "text": "合成图文字",
                    "confidence": 0.98,
                    "bbox": [1, 1, "NaN", 8],
                }
            ]
        return httpx.Response(200, json=payload)

    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(2, 1, 30.0, 1),
    )
    client = OcrClient(
        pool,
        revision=_REVISION,
        api_token=None,
        max_input_bytes=1024,
    )

    result = client.recognize(
        image,
        media_type="image/png",
        media_sha256=media_sha256,
    )

    assert result.confidence == 0.98
    assert calls == ["bad", "good"]


def test_ocr_all_invalid_endpoints_hide_response_and_media() -> None:
    image = _png()
    media_sha256 = hashlib.sha256(image).hexdigest()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        payload = _ocr_payload(media_sha256="0" * 64)
        payload["text"] = "不得泄露的响应正文"
        return httpx.Response(200, json=payload)

    pool = ResilientHttpPool(
        ("http://bad-a", "http://bad-b"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(2, 1, 30.0, 1),
    )
    client = OcrClient(
        pool,
        revision=_REVISION,
        api_token=None,
        max_input_bytes=1024,
    )

    with pytest.raises(
        ExternalServiceUnavailableError,
        match="INVALID_RESPONSE_SCHEMA",
    ) as error:
        client.recognize(
            image,
            media_type="image/png",
            media_sha256=media_sha256,
        )

    message = str(error.value)
    assert calls == ["bad-a", "bad-b"]
    assert "不得泄露的响应正文" not in message
    assert media_sha256 not in message


def test_emf_rasterizer_checks_converter_output() -> None:
    rasterizer = SandboxedEmfRasterizer(
        executable=Path("/bin/cp"),
        timeout_seconds=1.0,
        max_output_bytes=1024,
    )

    assert rasterizer.rasterize(_png()).startswith(b"\x89PNG")
    with pytest.raises(RasterizationError, match="NOT_PNG"):
        rasterizer.rasterize(b"synthetic emf")


def test_paddle_engine_freezes_static_server_model_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detection = tmp_path / "det"
    recognition = tmp_path / "rec"
    for model_dir in (detection, recognition):
        model_dir.mkdir()
        (model_dir / "inference.json").write_text("{}", encoding="utf-8")
        (model_dir / "inference.pdiparams").write_bytes(b"weights")
    captured: dict[str, object] = {}

    def paddleocr_factory(**options: object) -> object:
        captured.update(options)
        return SimpleNamespace(predict=lambda _: ())

    monkeypatch.setattr(
        "rag_app.ocr.paddle_engine.importlib.import_module",
        lambda _: SimpleNamespace(PaddleOCR=paddleocr_factory),
    )

    engine = PaddleOcrEngine.from_model_directories(
        detection_model_dir=detection,
        recognition_model_dir=recognition,
        device="gpu:0",
    )

    assert engine.ready()
    assert captured["engine"] == "paddle"
    assert captured["device"] == "gpu:0"
    assert captured["enable_hpi"] is False
    assert captured["use_tensorrt"] is False
    assert captured["enable_mkldnn"] is False
    assert captured["text_detection_model_name"] == "PP-OCRv5_server_det"
    assert captured["text_recognition_model_name"] == "PP-OCRv5_server_rec"


def test_paddle_engine_preserves_numpy_rec_boxes() -> None:
    result = SimpleNamespace(
        json={
            "res": {
                "rec_texts": ["坐标文本"],
                "rec_scores": [0.99],
                "rec_boxes": numpy.array([[1.2, 2.2, 10.7, 20.8]]),
            }
        }
    )
    engine = PaddleOcrEngine(
        SimpleNamespace(predict=lambda _: (result,))
    )

    recognized = engine.recognize(_png())

    assert recognized.lines[0].bbox == (1, 2, 11, 21)
