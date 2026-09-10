"""Product OCR 的远程/本地统一合同。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field, StrictInt

from rag_app.core.models import ProviderCall
from rag_app.core.models.common import FrozenModel


class ProductOcrPolicy(FrozenModel):
    """一次 Product OCR 构建使用的语义和资源策略。"""

    model: str = Field(
        default="qwen3.5-ocr",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    policy_version: str = Field(default="embedded-image-ocr-v1", max_length=64)
    egress_allowed: bool = False
    max_input_bytes: StrictInt = Field(default=2_097_152, gt=0, le=8_388_608)
    max_pixels: StrictInt = Field(default=1_048_576, gt=0)
    max_output_tokens: StrictInt = Field(default=4096, gt=0, le=4096)


class OcrAdapterIdentity(FrozenModel):
    """决定 OCR 内容与缓存兼容性的完整适配器身份。"""

    adapter: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=64)


class ProductOcrLine(FrozenModel):
    """OCR 返回的真实文本行；未知版面字段保持为空。"""

    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    bbox: tuple[int, int, int, int] | None = None


class ProductOcrInspection(FrozenModel):
    """发送识别请求前可在本地确认的媒体属性。"""

    media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    size_bytes: StrictInt = Field(gt=0)
    width: StrictInt | None = Field(default=None, gt=0)
    height: StrictInt | None = Field(default=None, gt=0)


class ProductOcrRecognition(FrozenModel):
    """本地与远程 OCR 都必须返回的可缓存识别结果。"""

    text: str = Field(min_length=1, repr=False)
    origin: str = "ocr"
    media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    adapter: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    bbox: tuple[int, int, int, int] | None = None
    lines: tuple[ProductOcrLine, ...] = ()
    call: ProviderCall | None = None
    complete: bool = True

    @property
    def adapter_identity(self) -> OcrAdapterIdentity:
        """返回与缓存键完全相同的适配器身份。

        Args:
            无参数；读取当前识别结果中的身份字段。

        Returns:
            完整的 OCR 适配器身份。

        """
        return OcrAdapterIdentity(
            adapter=self.adapter,
            provider=self.provider,
            revision=self.revision,
            model=self.model,
            policy_version=self.policy_version,
        )


class ProductOcrAdapter(Protocol):
    """Product enrichment 可使用的最小 OCR 适配器端口。"""

    @property
    def identity(self) -> OcrAdapterIdentity:
        """返回当前适配器的完整内容身份。

        Args:
            无参数；读取实现冻结的适配器配置。

        Returns:
            参与缓存与 Revision 的 OCR 适配器身份。

        """

    def inspect(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> ProductOcrInspection:
        """在发送请求前验证媒体边界。

        Args:
            media_bytes: 待检查的媒体字节。
            media_type: 声明的媒体 MIME 类型。
            media_sha256: 调用方提供的内容摘要。

        Returns:
            可在本地证明的媒体属性。

        """

    def recognize(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> ProductOcrRecognition:
        """识别一张受控媒体并返回统一结果。

        Args:
            media_bytes: 已通过边界检查的媒体字节。
            media_type: 媒体 MIME 类型。
            media_sha256: 媒体内容 SHA-256。

        Returns:
            带真实可选定位和调用信息的统一识别结果。

        """

    def close(self) -> None:
        """释放当前适配器持有的资源。

        Args:
            无参数；关闭当前适配器。

        Returns:
            无返回值。

        """


__all__ = [
    "OcrAdapterIdentity",
    "ProductOcrAdapter",
    "ProductOcrInspection",
    "ProductOcrLine",
    "ProductOcrPolicy",
    "ProductOcrRecognition",
]
