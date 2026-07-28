"""受资源限制的内部 OCR FastAPI 服务。"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import hashlib
import hmac
import io
import threading
import time
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException
from PIL import Image, UnidentifiedImageError

from rag_app.ocr.models import (
    OcrEngine,
    OcrEngineResult,
    OcrRequest,
    OcrResponse,
)
from rag_app.ocr.rasterize import EmfRasterizer, RasterizationError

__all__ = ["OcrLimits", "create_ocr_app"]

_BUSY_WAIT_SECONDS = 0.05
_MINIMUM_API_TOKEN_LENGTH = 32


@dataclass(frozen=True, slots=True)
class OcrLimits:
    """OCR API 的字节、像素、超时与并发硬上限。"""

    max_input_bytes: int
    max_pixels: int
    request_timeout_seconds: float
    max_concurrency: int

    def __post_init__(self) -> None:
        """拒绝零值、负值和无界配置。"""
        if min(
            self.max_input_bytes,
            self.max_pixels,
            self.max_concurrency,
        ) <= 0:
            raise ValueError("OCR 字节、像素和并发上限必须为正数。")
        if self.request_timeout_seconds <= 0:
            raise ValueError("OCR 请求超时必须为正数。")


class _OcrBusyError(RuntimeError):
    """OCR 并发闸门当前没有容量。"""


class _OcrTimeoutError(RuntimeError):
    """OCR 推理超过请求时限。"""


class _BoundedRunner:
    """限制并发并让超时任务继续占用闸门直到实际结束。"""

    def __init__(self, engine: OcrEngine, limits: OcrLimits) -> None:
        self._engine = engine
        self._limits = limits
        self._semaphore = threading.BoundedSemaphore(
            limits.max_concurrency
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=limits.max_concurrency,
            thread_name_prefix="rag-ocr",
        )

    def run(self, image_bytes: bytes) -> OcrEngineResult:
        """在闸门和超时内运行一次推理。

        Args:
            image_bytes: 待识别图片的原始字节。

        Returns:
            OCR 引擎输出的文本、置信度与版面结果。

        """
        if not self._semaphore.acquire(timeout=_BUSY_WAIT_SECONDS):
            raise _OcrBusyError
        future = self._executor.submit(self._engine.recognize, image_bytes)
        future.add_done_callback(lambda _: self._semaphore.release())
        try:
            return future.result(
                timeout=self._limits.request_timeout_seconds
            )
        except concurrent.futures.TimeoutError as error:
            raise _OcrTimeoutError from error


def create_ocr_app(
    *,
    engine: OcrEngine,
    revision: str,
    limits: OcrLimits,
    rasterizer: EmfRasterizer | None = None,
    api_token: str | None = None,
) -> FastAPI:
    """创建只接受固定 OCR revision 的内部服务。

    Args:
        engine: 已加载本地模型的 PaddleOCR 引擎。
        revision: 模型、运行时和配置共同确定的 revision。
        limits: 输入、像素、超时和并发硬上限。
        rasterizer: 可选的受限 EMF 光栅化实现。
        api_token: 可选内部 Bearer token。

    Returns:
        包含 live、ready 和 OCR 路由的 FastAPI 应用。

    Raises:
        ValueError: revision 或 API token 无效。

    """
    if not revision:
        raise ValueError("OCR revision 不能为空。")
    if (
        api_token is not None
        and len(api_token) < _MINIMUM_API_TOKEN_LENGTH
    ):
        raise ValueError("OCR API token 至少需要 32 字符。")
    runner = _BoundedRunner(engine, limits)
    app = FastAPI(
        title="rag-ocr",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def _live() -> dict[str, str]:
        return {"status": "live", "revision": revision}

    def _ready() -> dict[str, str]:
        if not engine.ready():
            raise HTTPException(status_code=503, detail="OCR_NOT_READY")
        return {"status": "ready", "revision": revision}

    def _recognize(
        request: OcrRequest,
        authorization: str | None = Header(default=None),
    ) -> OcrResponse:
        _authorize(authorization, api_token)
        if request.ocr_revision != revision:
            raise HTTPException(
                status_code=409,
                detail="OCR_REVISION_MISMATCH",
            )
        media_bytes = _decode_and_check_request(request, limits)
        normalized, width, height = _normalize_image(
            media_bytes,
            media_type=request.media_type,
            limits=limits,
            rasterizer=rasterizer,
        )
        started = time.perf_counter()
        try:
            result = runner.run(normalized)
        except _OcrBusyError as error:
            raise HTTPException(
                status_code=429,
                detail="OCR_BUSY",
            ) from error
        except _OcrTimeoutError as error:
            raise HTTPException(
                status_code=504,
                detail="OCR_TIMEOUT",
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail="OCR_INFERENCE_FAILED",
            ) from error
        confidence = (
            sum(line.confidence for line in result.lines)
            / len(result.lines)
            if result.lines
            else 0.0
        )
        return OcrResponse(
            media_sha256=request.media_sha256,
            ocr_revision=revision,
            text="\n".join(line.text for line in result.lines),
            confidence=confidence,
            lines=result.lines,
            width=width,
            height=height,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )

    app.add_api_route("/live", _live, methods=["GET"])
    app.add_api_route("/ready", _ready, methods=["GET"])
    app.add_api_route(
        "/v1/ocr",
        _recognize,
        methods=["POST"],
        response_model=OcrResponse,
    )
    return app


def _authorize(
    authorization: str | None,
    api_token: str | None,
) -> None:
    if api_token is None:
        return
    expected = f"Bearer {api_token}"
    if authorization is None or not hmac.compare_digest(
        authorization,
        expected,
    ):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def _decode_and_check_request(
    request: OcrRequest,
    limits: OcrLimits,
) -> bytes:
    try:
        media_bytes = base64.b64decode(
            request.content_base64,
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail="INVALID_BASE64",
        ) from error
    if not media_bytes or len(media_bytes) > limits.max_input_bytes:
        raise HTTPException(
            status_code=413,
            detail="OCR_INPUT_SIZE_LIMIT",
        )
    if hashlib.sha256(media_bytes).hexdigest() != request.media_sha256:
        raise HTTPException(
            status_code=422,
            detail="MEDIA_SHA256_MISMATCH",
        )
    return media_bytes


def _normalize_image(
    media_bytes: bytes,
    *,
    media_type: str,
    limits: OcrLimits,
    rasterizer: EmfRasterizer | None,
) -> tuple[bytes, int, int]:
    expected_format = "PNG" if media_type == "image/png" else "JPEG"
    if media_type == "image/emf":
        if rasterizer is None:
            raise HTTPException(
                status_code=415,
                detail="EMF_RASTERIZER_UNAVAILABLE",
            )
        try:
            media_bytes = rasterizer.rasterize(media_bytes)
        except RasterizationError as error:
            raise HTTPException(
                status_code=422,
                detail=str(error),
            ) from error
        expected_format = "PNG"
    try:
        with Image.open(io.BytesIO(media_bytes)) as image:
            if image.format != expected_format:
                raise HTTPException(
                    status_code=422,
                    detail="IMAGE_TYPE_MISMATCH",
                )
            width, height = image.size
            if width <= 0 or height <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="INVALID_IMAGE_DIMENSIONS",
                )
            if width * height > limits.max_pixels:
                raise HTTPException(
                    status_code=413,
                    detail="OCR_PIXEL_LIMIT",
                )
            image.load()
            normalized = image.convert("RGB")
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=False)
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail="INVALID_IMAGE",
        ) from error
    return output.getvalue(), width, height
