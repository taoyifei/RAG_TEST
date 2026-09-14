"""PaddleOCR 3.7.0 完整文档产线的自托管 PDF Adapter。"""

from __future__ import annotations

import base64
from typing import Self

import httpx
from pydantic import Field, field_validator

from rag_app.adapters.parsers.pdf.contracts import (
    normalize_paddle_pages,
    self_hosted_pages,
)
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import (
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    FrozenModel,
    ParseContext,
    ParseSource,
    PdfParseResult,
    PdfParserMode,
    PdfSourceInspection,
)
from rag_app.core.policies import ParsingPolicy

_PDF_MEDIA_TYPE = "application/pdf"
_MAX_RESPONSE_BYTES = 128 * 1024 * 1024
_HTTP_REDIRECT = 300
_HTTP_RATE_LIMITED = 429


class PaddleSelfHostedPdfConfig(FrozenModel):
    """不含默认裸 VLM 路径的完整产线连接配置。"""

    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(default="", max_length=4096, repr=False)
    model: str = Field(default="PaddleOCR-VL-1.6", min_length=1, max_length=160)
    parser_revision: str = Field(
        default="paddleocr-3.7.0-pipeline-v1.6",
        min_length=1,
        max_length=80,
    )
    request_timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)

    @field_validator("base_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = httpx.URL(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("PaddleOCR Base URL 必须是完整 HTTP(S) 地址。")
        if parsed.query or parsed.fragment or parsed.userinfo:
            raise ValueError("PaddleOCR Base URL 禁止查询、片段和用户信息。")
        return normalized


class PaddleSelfHostedPdfParser:
    """调用 `/layout-parsing` 并拒绝任何不完整页面集合。"""

    parser_capabilities = ParserCapabilities(
        supported_extensions=(".pdf",),
        supported_media_types=(_PDF_MEDIA_TYPE,),
        supports_tables=True,
        supports_images="partial",
        supports_numbering="partial",
    )

    def __init__(
        self,
        config: PaddleSelfHostedPdfConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """创建自托管 Adapter。

        Args:
            config: 完整产线地址、可选密钥和解析身份。
            client: 合同测试或共享运行时提供的 HTTP Client。

        Returns:
            无返回值。

        """
        self._config = config
        self._owned_client = client is None
        headers = (
            {"Authorization": f"Bearer {config.api_key}"}
            if config.api_key
            else None
        )
        self._client = client or httpx.Client(
            base_url=config.base_url,
            headers=headers,
            follow_redirects=False,
            timeout=config.request_timeout_seconds,
        )
        self._closed = False

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回不含地址和 Secret 的 Adapter 描述。"""
        return ComponentDescriptor(
            kind=ComponentKind.PARSER,
            name="paddle-self-hosted-pdf",
            version=self._config.parser_revision,
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                permits_network=True,
                formats=(_PDF_MEDIA_TYPE,),
            ),
        )

    @property
    def parser_mode(self) -> PdfParserMode:
        """返回自托管缓存域。"""
        return PdfParserMode.SELF_HOSTED

    @property
    def parser_model(self) -> str:
        """返回文档解析模型。"""
        return self._config.model

    @property
    def parser_revision(self) -> str:
        """返回完整产线实现修订。"""
        return self._config.parser_revision

    @property
    def parser_options_identity(self) -> str:
        """返回端点隔离且不含密钥的选项摘要。"""
        return canonical_sha256(
            {
                "endpoint": canonical_sha256(self._config.base_url),
                "fileType": 0,
                "useLayoutDetection": True,
                "prettifyMarkdown": False,
                "restructurePages": False,
                "returnMarkdownImages": False,
                "visualize": False,
            }
        )

    def parse_pdf(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
        inspection: PdfSourceInspection,
    ) -> PdfParseResult:
        """调用完整文档产线并按数组顺序绑定物理页。

        Args:
            source: 原始 PDF 字节。
            policy: 已在本地执行的资源边界。
            context: 当前 Job 的取消和进度边界。
            inspection: 本地证明的真实页数。

        Returns:
            覆盖全部物理页的规范化结果。

        Raises:
            ProviderUnavailable: 网络或服务暂时不可用。
            ProviderInvalidResponse: 返回页数或 JSON 合同无效。

        """
        del policy
        if self._closed:
            raise RuntimeError("Paddle PDF Adapter 已关闭。")
        if context.cancel_check is not None:
            context.cancel_check()
        request = self.request_payload(source.content)
        try:
            response = self._client.post(
                f"{self._config.base_url}/layout-parsing",
                json=request,
                timeout=self._config.request_timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ProviderUnavailable(
                "PaddleOCR 文档解析请求超时。",
                stage="pdf.paddle.self_hosted.transport",
                code="PADDLE_DOCUMENT_PARSE_TIMEOUT",
            ) from error
        except httpx.RequestError as error:
            raise ProviderUnavailable(
                "PaddleOCR 文档解析服务不可用。",
                stage="pdf.paddle.self_hosted.transport",
                code="PADDLE_DOCUMENT_PARSE_UNAVAILABLE",
                details={"error_type": type(error).__name__},
            ) from None
        self._raise_for_status(response)
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProviderInvalidResponse(
                "PaddleOCR 响应超过本地大小上限。",
                stage="pdf.paddle.self_hosted.response",
                code="PADDLE_RESPONSE_TOO_LARGE",
            )
        try:
            payload = response.json()
        except ValueError:
            raise ProviderInvalidResponse(
                "PaddleOCR 响应不是有效 JSON。",
                stage="pdf.paddle.self_hosted.response",
                code="PADDLE_RESPONSE_FORMAT_INVALID",
            ) from None
        raw_pages = self_hosted_pages(payload)
        if (
            len(raw_pages) != inspection.page_count
            and context.page_progress is not None
        ):
            observed = min(len(raw_pages), inspection.page_count)
            failed = (
                tuple(range(observed, inspection.page_count))
                if len(raw_pages) < inspection.page_count
                else ()
            )
            context.page_progress(
                observed,
                failed,
                len(raw_pages) < inspection.page_count,
            )
        self._require_complete_page_count(len(raw_pages), inspection.page_count)
        pages = normalize_paddle_pages(raw_pages, inspection)
        if context.page_progress is not None:
            context.page_progress(len(pages), (), False)
        return PdfParseResult(
            source_sha256=inspection.source_sha256,
            parser_mode=PdfParserMode.SELF_HOSTED,
            parser_model=self.parser_model,
            parser_revision=self.parser_revision,
            parser_options_identity=self.parser_options_identity,
            page_count=inspection.page_count,
            pages=pages,
        )

    @staticmethod
    def request_payload(content: bytes) -> dict[str, object]:
        """构造可由合同测试直接断言的 3.7.0 请求。"""
        return {
            "file": base64.b64encode(content).decode("ascii"),
            "fileType": 0,
            "useLayoutDetection": True,
            "prettifyMarkdown": False,
            "restructurePages": False,
            "returnMarkdownImages": False,
            "visualize": False,
        }

    def close(self) -> None:
        """只关闭由当前 Adapter 创建的 Client。"""
        if self._closed:
            return
        self._closed = True
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> Self:
        """进入当前 Adapter 资源作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开资源作用域并关闭自有 Client。"""
        del args
        self.close()

    @staticmethod
    def _require_complete_page_count(observed: int, expected: int) -> None:
        if observed == expected:
            return
        truncated = observed < expected
        raise ProviderInvalidResponse(
            "PaddleOCR 返回页数与原 PDF 物理页数不一致。",
            stage="pdf.paddle.self_hosted.pages",
            code=(
                "PDF_PARSER_TRUNCATED"
                if truncated
                else "PDF_PAGE_COUNT_MISMATCH"
            ),
            details={
                "expected_page_count": expected,
                "observed_page_count": observed,
            },
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < _HTTP_REDIRECT:
            return
        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError(
                "PaddleOCR 文档解析鉴权失败。",
                stage="pdf.paddle.self_hosted.http",
                code="PADDLE_AUTHENTICATION_FAILED",
            )
        if response.status_code == _HTTP_RATE_LIMITED:
            raise ProviderRateLimited(
                "PaddleOCR 文档解析触发限流。",
                stage="pdf.paddle.self_hosted.http",
                code="PADDLE_RATE_LIMITED",
            )
        raise ProviderUnavailable(
            "PaddleOCR 文档解析服务返回失败状态。",
            stage="pdf.paddle.self_hosted.http",
            code="PADDLE_DOCUMENT_PARSE_UNAVAILABLE",
            details={"http_status": response.status_code},
        )


__all__ = ["PaddleSelfHostedPdfConfig", "PaddleSelfHostedPdfParser"]
