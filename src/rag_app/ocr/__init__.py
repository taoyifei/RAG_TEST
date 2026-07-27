"""PaddleOCR 内部服务与应用侧客户端。"""

from rag_app.ocr.client import OcrClient
from rag_app.ocr.models import (
    DEFAULT_OCR_REVISION,
    OcrEngineResult,
    OcrLine,
    OcrRequest,
    OcrResponse,
)

__all__ = [
    "DEFAULT_OCR_REVISION",
    "OcrClient",
    "OcrEngineResult",
    "OcrLine",
    "OcrRequest",
    "OcrResponse",
]
