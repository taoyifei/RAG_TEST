"""启动固定模型和单 GPU 的 rag-ocr 服务。"""

from __future__ import annotations

import importlib
import os
from typing import Any, cast


def main() -> None:
    """从环境加载离线 OCR 服务并阻塞运行。

    Args:
        无参数。

    Returns:
        服务停止后返回 `None`。

    Raises:
        ValueError: revision 或本地模型/转换器资产不完整。

    """
    # OCR 包必须能在最小 Python 3.10 源码树中被导入，运行时依赖只在启动时解析。
    uvicorn = cast(Any, importlib.import_module("uvicorn"))
    models = cast(
        Any,
        importlib.import_module("rag_app.ocr.models"),
    )
    paddle_engine = cast(
        Any,
        importlib.import_module("rag_app.ocr.paddle_engine"),
    )
    rasterize = cast(
        Any,
        importlib.import_module("rag_app.ocr.rasterize"),
    )
    service = cast(
        Any,
        importlib.import_module("rag_app.ocr.service"),
    )
    settings_module = cast(
        Any,
        importlib.import_module("rag_app.ocr.settings"),
    )

    settings = settings_module.OcrServiceSettings()
    if settings.revision != models.DEFAULT_OCR_REVISION:
        raise ValueError("OCR revision 与固定实现不一致。")
    os.environ.setdefault(
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
        "True",
    )
    engine = paddle_engine.PaddleOcrEngine.from_model_directories(
        detection_model_dir=settings.detection_model_dir,
        recognition_model_dir=settings.recognition_model_dir,
        device=settings.device,
    )
    rasterizer = (
        None
        if settings.emf_converter is None
        else rasterize.SandboxedEmfRasterizer(
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
    app = service.create_ocr_app(
        engine=engine,
        revision=settings.revision,
        limits=service.OcrLimits(
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
