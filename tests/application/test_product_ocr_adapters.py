"""Product 本地/远程 OCR 统一端口的离线合同测试。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.core.errors import ConfigurationError
from rag_app.ocr import OcrClient
from rag_app.product.ocr_adapters import (
    LOCAL_OCR_CONNECTION_ID,
    LocalOcrAdapterConfig,
    LocalProductOcrAdapter,
)
from rag_app.product.ocr_contract import ProductOcrPolicy
from rag_app.product.provider_runtime import ProviderRuntimeRegistry
from tests.product_support import build_product_harness


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (128, 64), color="white").save(stream, format="PNG")
    return stream.getvalue()


def _local_adapter(
    revision: str,
) -> tuple[LocalProductOcrAdapter, httpx.Client, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "media_sha256": payload["media_sha256"],
                "ocr_revision": revision,
                "text": "压力 3.14 MPa\n模糊行",
                "confidence": 0.875,
                "lines": [
                    {
                        "text": "压力 3.14 MPa",
                        "confidence": 0.875,
                        "bbox": [11, 13, 97, 31],
                    },
                    {
                        "text": "模糊行",
                        "confidence": 0.5,
                        "bbox": [0, 0, 0, 0],
                    },
                ],
                "width": 128,
                "height": 64,
                "elapsed_ms": 17,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    pool = ResilientHttpPool(
        ("http://local-ocr:8090",),
        client=http_client,
        policy=ResiliencePolicy(
            max_attempts=1,
            failure_threshold=1,
            cooldown_seconds=1.0,
            max_concurrency=1,
        ),
    )
    client = OcrClient(
        pool,
        revision=revision,
        api_token=None,
        max_input_bytes=1_048_576,
    )
    adapter = LocalProductOcrAdapter(
        LocalOcrAdapterConfig(
            revision=revision,
            model="pp-ocrv5-server",
            max_input_bytes=1_048_576,
        ),
        client,
    )
    return adapter, http_client, requests


def test_local_adapter_reuses_ocr_client_and_preserves_real_line_geometry() -> (
    None
):
    revision = "paddleocr-offline-contract-v1"
    adapter, client, requests = _local_adapter(revision)
    data = _png()
    sha = hashlib.sha256(data).hexdigest()
    try:
        result = adapter.recognize(
            data, media_type="image/png", media_sha256=sha
        )
    finally:
        client.close()

    assert len(requests) == 1
    assert result.adapter_identity == adapter.identity
    assert result.revision == revision
    assert result.confidence == 0.875
    assert result.bbox is None
    assert result.lines[0].confidence == 0.875
    assert result.lines[0].bbox == (11, 13, 97, 31)
    assert result.lines[1].confidence == 0.5
    assert result.lines[1].bbox is None
    assert result.call is None


def test_registry_local_selection_is_injectable_and_unavailable_is_explicit(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    revision = "paddleocr-offline-contract-v1"
    adapter, client, _ = _local_adapter(revision)
    try:
        with pytest.raises(ConfigurationError) as caught:
            harness.runtime.providers.ocr_adapter(
                LOCAL_OCR_CONNECTION_ID,
                model="pp-ocrv5-server",
                config=ProductOcrPolicy(model="pp-ocrv5-server"),
            )
        assert caught.value.code == "LOCAL_OCR_UNAVAILABLE"

        registry = ProviderRuntimeRegistry(
            harness.runtime.credentials,
            harness.runtime.control,
            local_ocr_adapter=adapter,
        )
        selected = registry.ocr_adapter(
            LOCAL_OCR_CONNECTION_ID,
            model="pp-ocrv5-server",
            config=ProductOcrPolicy(model="pp-ocrv5-server"),
        )
        assert selected is adapter
        assert (
            registry.ocr_identity(
                LOCAL_OCR_CONNECTION_ID,
                model="pp-ocrv5-server",
                policy_version="embedded-image-ocr-v1",
            ).revision
            == revision
        )
    finally:
        client.close()
        harness.close()
