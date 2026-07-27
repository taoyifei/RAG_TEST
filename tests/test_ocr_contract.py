from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
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
