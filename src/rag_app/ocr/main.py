"""启动固定模型和单 GPU 的 rag-ocr 服务。"""

from __future__ import annotations

import os

import uvicorn

from rag_app.ocr.models import DEFAULT_OCR_REVISION
from rag_app.ocr.paddle_engine import PaddleOcrEngine
from rag_app.ocr.rasterize import SandboxedEmfRasterizer
from rag_app.ocr.service import OcrLimits, create_ocr_app
from rag_app.ocr.settings import OcrServiceSettings


def main() -> None:
    """从环境加载离线 OCR 服务并阻塞运行。

    Args:
        无参数。

    Returns:
        服务停止后返回 `None`。

    Raises:
        ValueError: revision 或本地模型/转换器资产不完整。

    """
    settings = OcrServiceSettings()
    if settings.revision != DEFAULT_OCR_REVISION:
        raise ValueError("OCR revision 与固定实现不一致。")
    os.environ.setdefault(
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
        "True",
    )
    engine = PaddleOcrEngine.from_model_directories(
        detection_model_dir=settings.detection_model_dir,
        recognition_model_dir=settings.recognition_model_dir,
        device=settings.device,
    )
    rasterizer = (
        None
        if settings.emf_converter is None
        else SandboxedEmfRasterizer(
            executable=settings.emf_converter,
            timeout_seconds=settings.emf_timeout_seconds,
            max_output_bytes=settings.emf_max_output_bytes,
        )
    )
    api_token = (
        None
        if settings.api_token is None
        else settings.api_token.get_secret_value()
    )
    app = create_ocr_app(
        engine=engine,
        revision=settings.revision,
        limits=OcrLimits(
            max_input_bytes=settings.max_input_bytes,
            max_pixels=settings.max_pixels,
            request_timeout_seconds=settings.request_timeout_seconds,
            max_concurrency=settings.max_concurrency,
        ),
        rasterizer=rasterizer,
        api_token=api_token,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
