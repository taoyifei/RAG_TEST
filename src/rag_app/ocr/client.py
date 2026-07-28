"""应用与索引 worker 调用内部 OCR 服务的严格客户端。"""

from __future__ import annotations

import base64
import hashlib

from rag_app.clients.resilience import ResilientHttpPool
from rag_app.ocr.models import OcrRequest, OcrResponse

__all__ = ["OcrClient"]


class OcrClient:
    """使用媒体摘要和 OCR revision 调用内部 OCR 服务。"""

    def __init__(
        self,
        pool: ResilientHttpPool,
        *,
        revision: str,
        api_token: str | None,
        max_input_bytes: int,
    ) -> None:
        """冻结客户端缓存键与输入上限。

        Args:
            pool: OCR 专属超时、重试、熔断和并发端点池。
            revision: manifest 中固定的 OCR revision。
            api_token: 可选内部 Bearer token。
            max_input_bytes: 客户端允许发送的最大原始媒体字节数。

        Raises:
            ValueError: revision 为空或输入上限不是正整数。

        """
        if not revision:
            raise ValueError("OCR revision 不能为空。")
        if max_input_bytes <= 0:
            raise ValueError("OCR 输入上限必须为正整数。")
        self._pool = pool
        self._revision = revision
        self._max_input_bytes = max_input_bytes
        self._headers = (
            None
            if api_token is None
            else {"Authorization": f"Bearer {api_token}"}
        )

    def recognize(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> OcrResponse:
        """识别一个媒体并严格校验缓存键和响应 schema。

        Args:
            media_bytes: DOCX 中提取的原始媒体字节。
            media_type: 允许的 PNG、JPEG 或 EMF MIME 类型。
            media_sha256: 解析阶段计算的媒体 SHA256。

        Returns:
            与请求媒体和 revision 完全匹配的 OCR 结果。

        Raises:
            ValueError: 输入摘要、大小或类型不符合请求契约。
            ExternalRequestRejectedError: OCR 服务明确拒绝请求。
            ExternalServiceUnavailableError: 所有端点均不可用或响应无效。

        """
        if not media_bytes or len(media_bytes) > self._max_input_bytes:
            raise ValueError("OCR 媒体为空或超过客户端字节上限。")
        calculated = hashlib.sha256(media_bytes).hexdigest()
        if calculated != media_sha256:
            raise ValueError("OCR 媒体 SHA256 与解析结果不一致。")
        request = OcrRequest(
            media_sha256=media_sha256,
            ocr_revision=self._revision,
            media_type=media_type,
            content_base64=base64.b64encode(media_bytes).decode("ascii"),
        )

        def _validate_response(payload: object) -> object:
            result = OcrResponse.model_validate(payload)
            if (
                result.media_sha256 != media_sha256
                or result.ocr_revision != self._revision
            ):
                raise ValueError("OCR 响应缓存键与请求不一致。")
            return result

        response = self._pool.request_json(
            "POST",
            "/v1/ocr",
            payload=request.model_dump(mode="json"),
            headers=self._headers,
            validator=_validate_response,
        )
        return OcrResponse.model_validate(response.payload)
