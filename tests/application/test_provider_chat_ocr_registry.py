"""验证已有加密连接经产品 Registry 进入新 Chat/OCR 主适配器。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from rag_app.adapters.providers.aliyun_chat import ChatMessage
from rag_app.core.models import ProviderCall
from rag_app.product.models import ProviderConnectionDraft
from tests.product_support import build_product_harness


@pytest.mark.parametrize(
    ("operation", "model"),
    [
        ("generation", "qwen3.7-flash"),
        ("query.rewrite", "qwen3.7-flash"),
        ("image.ocr", "qwen3.5-ocr"),
    ],
)
def test_registry_validates_and_executes_the_same_chat_protocol(
    tmp_path: Path,
    operation: str,
    model: str,
):
    requests: list[httpx.Request] = []

    def transport(_connection: object) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": payload["model"],
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "合成资料编号314",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 21,
                        "completion_tokens": 4,
                        "total_tokens": 25,
                    },
                },
            )

        return httpx.MockTransport(handler)

    harness = build_product_harness(tmp_path, transport_factory=transport)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio",
            "synthetic-provider-secret",
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="合成百炼连接",
                provider_type="aliyun-model-studio",
                credential_id=credential.credential_id,
                workspace_id="synthetic",
                endpoint_mode="beijing_dashscope",
                region="cn-beijing",
            )
        )
        run = harness.runtime.providers.validate(
            connection.connection_id,
            operation=operation,
            model=model,
        )
        assert run.status == "succeeded" and run.validation_mode == "mock"
        assert run.observed_tokens == 25 and run.dimension is None
        assert requests[0].extensions["rag_chat_operation"] == operation
        call: ProviderCall
        if operation == "image.ocr":
            adapter = harness.runtime.providers.ocr_adapter(
                connection.connection_id, model=model
            )
            image = Image.new("RGB", (128, 128), color="white")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            data = buffer.getvalue()
            try:
                result = adapter.recognize(
                    data,
                    media_type="image/png",
                    media_sha256=hashlib.sha256(data).hexdigest(),
                )
                call = result.call
                assert result.confidence is None
            finally:
                adapter.close()
        else:
            chat = harness.runtime.providers.chat_adapter(
                connection.connection_id, model=model
            )
            try:
                completed = chat.complete(
                    (ChatMessage(role="user", content="合成资料问答"),)
                )
                call = completed.call
            finally:
                chat.close()
        assert call.observed_tokens == 25 and len(requests) == 2
        assert all(
            str(item.url)
            == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            for item in requests
        )
        assert "synthetic-provider-secret" not in call.model_dump_json()
    finally:
        harness.close()
