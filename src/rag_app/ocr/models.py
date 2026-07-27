"""OCR 服务共享的严格请求、响应和引擎契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_OCR_REVISION",
    "OcrEngine",
    "OcrEngineResult",
    "OcrLine",
    "OcrRequest",
    "OcrResponse",
]

DEFAULT_OCR_REVISION = (
    "paddleocr-3.5.0-ppocrv5-server-det-rec-paddle-static"
)


class OcrLine(BaseModel):
    """一行带置信度和像素框的 OCR 文本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class OcrEngineResult:
    """OCR 引擎返回的有序文本行。"""

    lines: tuple[OcrLine, ...]


class OcrEngine(Protocol):
    """服务端推理引擎的最小接口。"""

    def ready(self) -> bool:
        """返回本地模型是否已加载且可接受请求。

        Args:
            无参数。

        Returns:
            模型和运行时已就绪时返回 `True`。

        """

    def recognize(self, image_bytes: bytes) -> OcrEngineResult:
        """识别一张已验证并规范化为 PNG 的图片。

        Args:
            image_bytes: 服务层规范化后的 PNG 字节。

        Returns:
            按原始输出顺序排列的 OCR 文本行。

        """


class OcrRequest(BaseModel):
    """一次带稳定缓存键的 OCR 请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ocr_revision: str = Field(min_length=1, max_length=160)
    media_type: str = Field(pattern=r"^image/(?:png|jpeg|emf)$")
    content_base64: str = Field(min_length=1, max_length=32_000_000)


class OcrResponse(BaseModel):
    """OCR 服务返回的规范结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ocr_revision: str = Field(min_length=1, max_length=160)
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    lines: tuple[OcrLine, ...]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    elapsed_ms: int = Field(ge=0)
