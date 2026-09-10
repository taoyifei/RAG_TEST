"""把百炼和内部 PaddleOCR 映射到 Product OCR 统一合同。"""

from __future__ import annotations

import hashlib
import io
import warnings

from PIL import Image, UnidentifiedImageError
from pydantic import Field, StrictInt

from rag_app.adapters.providers.aliyun_ocr import (
    AliyunOcrAdapter,
    AliyunOcrConfig,
    inspect_ocr_image,
)
from rag_app.core.models.common import FrozenModel
from rag_app.ocr.client import OcrClient
from rag_app.ocr.models import DEFAULT_OCR_REVISION
from rag_app.product.ocr_contract import (
    OcrAdapterIdentity,
    ProductOcrInspection,
    ProductOcrLine,
    ProductOcrPolicy,
    ProductOcrRecognition,
)

LOCAL_OCR_CONNECTION_ID = "local-ocr"
_ALIYUN_ADAPTER = "aliyun-multimodal-ocr"
_ALIYUN_ADAPTER_REVISION = "aliyun-ocr-adapter-v1"
_LOCAL_ADAPTER = "local-paddleocr"
_LOCAL_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/emf"})
_FORMAT_BY_MIME = {"image/png": "PNG", "image/jpeg": "JPEG"}


class LocalOcrAdapterConfig(FrozenModel):
    """已由组合根验证并注入的本地 OCR 客户端身份与上限。"""

    connection_id: str = Field(
        default=LOCAL_OCR_CONNECTION_ID, min_length=1, max_length=200
    )
    provider: str = Field(default="rag-ocr", min_length=1, max_length=200)
    revision: str = Field(
        default=DEFAULT_OCR_REVISION, min_length=1, max_length=200
    )
    model: str = Field(default="pp-ocrv5-server", min_length=1, max_length=160)
    policy_version: str = Field(
        default="embedded-image-ocr-v1", min_length=1, max_length=64
    )
    max_input_bytes: StrictInt = Field(default=10 * 1024 * 1024, gt=0)
    max_pixels: StrictInt = Field(default=40_000_000, gt=0)


class AliyunProductOcrAdapter:
    """保留百炼真实计量，同时补齐 Product 适配器身份。"""

    def __init__(
        self,
        adapter: AliyunOcrAdapter,
        *,
        provider: str,
        policy: ProductOcrPolicy,
    ) -> None:
        self._adapter = adapter
        self._policy = policy
        self._identity = OcrAdapterIdentity(
            adapter=_ALIYUN_ADAPTER,
            provider=provider,
            revision=_ALIYUN_ADAPTER_REVISION,
            model=policy.model,
            policy_version=policy.policy_version,
        )

    @property
    def identity(self) -> OcrAdapterIdentity:
        """返回百炼连接与适配器实现的缓存身份。

        Args:
            无参数；读取构造时冻结的身份。

        Returns:
            参与 OCR 缓存键的完整适配器身份。

        """
        return self._identity

    def inspect(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> ProductOcrInspection:
        """使用百炼发送前的同一图片策略执行检查。

        Args:
            media_bytes: 待检查的受控图片字节。
            media_type: 声明的图片 MIME 类型。
            media_sha256: 调用方计算的图片 SHA-256。

        Returns:
            通过字节、类型、摘要和像素边界检查的媒体信息。

        """
        image = inspect_ocr_image(
            media_bytes,
            media_type=media_type,
            media_sha256=media_sha256,
            config=_aliyun_config(self._policy),
        )
        return ProductOcrInspection(
            media_sha256=image.media_sha256,
            media_type=image.media_type,
            size_bytes=image.size_bytes,
            width=image.width,
            height=image.height,
        )

    def recognize(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> ProductOcrRecognition:
        """映射百炼结果；供应商未返回的框和置信度保持 None。

        Args:
            media_bytes: 已受控的图片字节。
            media_type: 图片 MIME 类型。
            media_sha256: 图片内容 SHA-256。

        Returns:
            带实际 Provider 计量和可选定位的统一 OCR 结果。

        """
        result = self._adapter.recognize(
            media_bytes,
            media_type=media_type,
            media_sha256=media_sha256,
        )
        return ProductOcrRecognition(
            text=result.text,
            media_sha256=result.media_sha256,
            media_type=result.media_type,
            width=result.width,
            height=result.height,
            **self.identity.model_dump(),
            confidence=result.confidence,
            bbox=result.bbox,
            call=result.call,
            complete=result.complete,
        )

    def close(self) -> None:
        """关闭百炼适配器持有的 HTTP 客户端。

        Args:
            无参数；释放当前适配器资源。

        Returns:
            无返回值。

        """
        self._adapter.close()


class LocalProductOcrAdapter:
    """复用严格 OcrClient，并保留服务返回的真实行级信息。"""

    def __init__(
        self, config: LocalOcrAdapterConfig, client: OcrClient
    ) -> None:
        self.config = config
        self._client = client
        self._identity = OcrAdapterIdentity(
            adapter=_LOCAL_ADAPTER,
            provider=config.provider,
            revision=config.revision,
            model=config.model,
            policy_version=config.policy_version,
        )

    @property
    def connection_id(self) -> str:
        """返回 Product settings 用于选择本地服务的稳定标识。

        Args:
            无参数；读取注入配置。

        Returns:
            本地 OCR 连接 ID。

        """
        return self.config.connection_id

    @property
    def identity(self) -> OcrAdapterIdentity:
        """返回本地服务 provider、revision 与策略身份。

        Args:
            无参数；读取构造时冻结的身份。

        Returns:
            参与 OCR 缓存与 Revision 的适配器身份。

        """
        return self._identity

    def inspect(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> ProductOcrInspection:
        """在进入 OcrClient 前验证摘要、字节、格式和已知像素。

        Args:
            media_bytes: 待识别媒体字节。
            media_type: 声明的媒体 MIME 类型。
            media_sha256: 调用方提供的内容摘要。

        Returns:
            验证后的字节数和可证明图像尺寸。

        """
        return _inspect_local_media(
            media_bytes,
            media_type=media_type,
            media_sha256=media_sha256,
            config=self.config,
        )

    def recognize(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> ProductOcrRecognition:
        """调用内部服务并逐字段保留它实际返回的置信度和框。

        Args:
            media_bytes: 已受控的媒体字节。
            media_type: 媒体 MIME 类型。
            media_sha256: 媒体内容 SHA-256。

        Returns:
            版本匹配且非空的统一 OCR 识别结果。

        """
        self.inspect(
            media_bytes,
            media_type=media_type,
            media_sha256=media_sha256,
        )
        result = self._client.recognize(
            media_bytes,
            media_type=media_type,
            media_sha256=media_sha256,
        )
        if result.ocr_revision != self.config.revision:
            raise ValueError("OCR_REVISION_MISMATCH")
        if not result.text:
            raise ValueError("OCR_OUTPUT_EMPTY")
        return ProductOcrRecognition(
            text=result.text,
            media_sha256=result.media_sha256,
            media_type=media_type,
            width=result.width,
            height=result.height,
            **self.identity.model_dump(),
            confidence=result.confidence,
            # 服务没有返回整图 bbox；只有行级 bbox 可被保留。
            bbox=None,
            lines=tuple(
                ProductOcrLine(
                    text=line.text,
                    confidence=line.confidence,
                    # 现有 service 用全零框表达缺失，Product 合同还原为 None。
                    bbox=None if line.bbox == (0, 0, 0, 0) else line.bbox,
                )
                for line in result.lines
            ),
        )

    def close(self) -> None:
        """客户端生命周期由注入方所有，单次识别不关闭共享连接池。

        Args:
            无参数；本适配器没有独占资源需要释放。

        Returns:
            无返回值。

        """


def aliyun_ocr_identity(
    *, provider: str, policy: ProductOcrPolicy
) -> OcrAdapterIdentity:
    """不创建 HTTP Client 即计算百炼 OCR 内容身份。

    Args:
        provider: 当前百炼连接的 Provider ID。
        policy: 统一 Product OCR 策略。

    Returns:
        可在组合阶段参与内容和缓存身份的适配器身份。

    """
    return OcrAdapterIdentity(
        adapter=_ALIYUN_ADAPTER,
        provider=provider,
        revision=_ALIYUN_ADAPTER_REVISION,
        model=policy.model,
        policy_version=policy.policy_version,
    )


def _aliyun_config(policy: ProductOcrPolicy) -> AliyunOcrConfig:
    return AliyunOcrConfig(
        model=policy.model,
        policy_version=policy.policy_version,
        egress_allowed=policy.egress_allowed,
        max_input_bytes=policy.max_input_bytes,
        max_pixels=policy.max_pixels,
        max_output_tokens=policy.max_output_tokens,
    )


def aliyun_product_ocr_adapter(
    adapter: AliyunOcrAdapter,
    *,
    provider: str,
    policy: ProductOcrPolicy,
) -> AliyunProductOcrAdapter:
    """把已安全构造的百炼适配器包装为 Product 端口。

    Args:
        adapter: 已配置凭据和传输边界的百炼适配器。
        provider: 当前连接的 Provider ID。
        policy: 统一 Product OCR 策略。

    Returns:
        实现 Product OCR 合同的百炼包装器。

    """
    return AliyunProductOcrAdapter(
        adapter,
        provider=provider,
        policy=policy,
    )


def aliyun_ocr_config(policy: ProductOcrPolicy) -> AliyunOcrConfig:
    """把统一策略映射为百炼适配器配置。

    Args:
        policy: 已验证的 Product OCR 策略。

    Returns:
        字节、像素、输出和出网边界一致的百炼配置。

    """
    return _aliyun_config(policy)


def _inspect_local_media(
    media_bytes: bytes,
    *,
    media_type: str,
    media_sha256: str,
    config: LocalOcrAdapterConfig,
) -> ProductOcrInspection:
    if media_type not in _LOCAL_MEDIA_TYPES:
        raise ValueError("OCR_MEDIA_UNSUPPORTED")
    if not media_bytes or len(media_bytes) > config.max_input_bytes:
        raise ValueError("OCR_IMAGE_BYTE_LIMIT")
    digest = hashlib.sha256(media_bytes).hexdigest()
    if digest != media_sha256:
        raise ValueError("OCR_MEDIA_HASH_MISMATCH")
    if media_type == "image/emf":
        # EMF 只由服务端受限光栅化器解析，Product 进程不预读其版面。
        return ProductOcrInspection(
            media_sha256=digest,
            media_type=media_type,
            size_bytes=len(media_bytes),
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(media_bytes)) as image:
                width, height = image.size
                if image.format != _FORMAT_BY_MIME[media_type]:
                    raise ValueError("OCR_MEDIA_MIME_MISMATCH")
                if width <= 0 or height <= 0:
                    raise ValueError("OCR_IMAGE_INVALID")
                if width * height > config.max_pixels:
                    raise ValueError("OCR_IMAGE_PIXEL_LIMIT")
                image.verify()
    except ValueError:
        raise
    except (
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ValueError("OCR_IMAGE_INVALID") from None
    return ProductOcrInspection(
        media_sha256=digest,
        media_type=media_type,
        size_bytes=len(media_bytes),
        width=width,
        height=height,
    )


__all__ = [
    "LOCAL_OCR_CONNECTION_ID",
    "AliyunProductOcrAdapter",
    "LocalOcrAdapterConfig",
    "LocalProductOcrAdapter",
    "aliyun_ocr_config",
    "aliyun_ocr_identity",
    "aliyun_product_ocr_adapter",
]
