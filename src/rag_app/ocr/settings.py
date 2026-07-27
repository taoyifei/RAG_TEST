"""独立 OCR 服务的环境配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_app.ocr.models import DEFAULT_OCR_REVISION

__all__ = ["OcrServiceSettings"]


class OcrServiceSettings(BaseSettings):
    """PaddleOCR 模型路径、设备和资源上限。"""

    model_config = SettingsConfigDict(
        env_prefix="RAG_OCR_",
        extra="forbid",
    )

    revision: str = DEFAULT_OCR_REVISION
    detection_model_dir: Path = Path(
        "/opt/rag-ocr/models/PP-OCRv5_server_det_infer"
    )
    recognition_model_dir: Path = Path(
        "/opt/rag-ocr/models/PP-OCRv5_server_rec_infer"
    )
    device: str = "gpu:0"
    api_token: SecretStr | None = None
    max_input_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_pixels: int = Field(default=40_000_000, gt=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_concurrency: int = Field(default=1, gt=0, le=1)
    emf_converter: Path | None = None
    emf_timeout_seconds: float = Field(default=10.0, gt=0)
    emf_max_output_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    host: str = "0.0.0.0"  # noqa: S104
    port: int = Field(default=8090, ge=1, le=65535)
