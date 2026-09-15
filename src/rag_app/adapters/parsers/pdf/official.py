"""PaddleOCR 官方托管 API 的 PDF Adapter。"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import Field

from rag_app.adapters.parsers.pdf.contracts import (
    normalize_paddle_pages,
    official_pages,
    provider_job_id,
)
from rag_app.adapters.parsers.pdf.official_api_client import (
    OfficialPaddleOcrApiClient,
)
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import (
    ConfigurationError,
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
    PdfPage,
    PdfParseResult,
    PdfParserMode,
    PdfSourceInspection,
)
from rag_app.core.policies import ParsingPolicy

OfficialClientFactory = Callable[[str, float, float], object]
_PDF_MEDIA_TYPE = "application/pdf"
_RETRYABLE_CLIENT_ERRORS = frozenset(
    {
        "NetworkError",
        "RequestTimeoutError",
        "ServiceUnavailableError",
    }
)


class PaddleOfficialApiPdfConfig(FrozenModel):
    """官方托管 API 的最小配置；Token 永不进入身份或 repr。"""

    access_token: str = Field(min_length=1, max_length=4096, repr=False)
    model: str = Field(default="PaddleOCR-VL-1.6", min_length=1, max_length=160)
    parser_revision: str = Field(
        default="paddleocr-official-api-3.7.0-http-v1",
        min_length=1,
        max_length=80,
    )
    request_timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)
    poll_timeout_seconds: float = Field(default=600.0, gt=0.0, le=7200.0)


class PaddleOfficialApiPdfParser:
    """按官方 3.7.0 API 合同提交、轮询和解析 PDF。"""

    parser_capabilities = ParserCapabilities(
        supported_extensions=(".pdf",),
        supported_media_types=(_PDF_MEDIA_TYPE,),
        supports_tables=True,
        supports_images="partial",
        supports_numbering="partial",
    )

    def __init__(
        self,
        config: PaddleOfficialApiPdfConfig,
        *,
        client_factory: OfficialClientFactory | None = None,
    ) -> None:
        """创建官方 API Adapter。

        Args:
            config: Access Token、超时和模型身份。
            client_factory: 合同测试可注入的官方客户端工厂。

        Returns:
            无返回值。

        """
        self._config = config
        self._client_factory = client_factory or _official_client

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回不含 Token 的官方 API 描述。"""
        return ComponentDescriptor(
            kind=ComponentKind.PARSER,
            name="paddle-official-pdf",
            version=self.parser_revision,
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                permits_network=True,
                formats=(_PDF_MEDIA_TYPE,),
            ),
        )

    @property
    def parser_mode(self) -> PdfParserMode:
        """返回官方 API 缓存域。"""
        return PdfParserMode.OFFICIAL_API

    @property
    def parser_model(self) -> str:
        """返回文档解析模型。"""
        return self._config.model

    @property
    def parser_revision(self) -> str:
        """返回官方 API 合同修订。"""
        return self._config.parser_revision

    @property
    def parser_options_identity(self) -> str:
        """返回不含 Token 和超时的语义选项摘要。"""
        return canonical_sha256(
            {
                "model": self.parser_model,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_layout_detection": True,
                "use_chart_recognition": False,
                "prettify_markdown": False,
                "restructure_pages": False,
                "return_markdown_images": False,
                "visualize": False,
                "page_binding": "physical-order-with-single-page-recovery-v1",
            }
        )

    def parse_pdf(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
        inspection: PdfSourceInspection,
    ) -> PdfParseResult:
        """先整份解析，页数不符时按一基 `page_ranges` 有界恢复。

        Args:
            source: 原始 PDF 字节。
            policy: 本地资源边界。
            context: 当前 Job 的取消和进度边界。
            inspection: 本地证明的物理页面集合。

        Returns:
            物理页从零连续、没有静默缺页的规范化结果。

        Raises:
            ProviderInvalidResponse: 官方结果最终仍不完整。
            ProviderUnavailable: 官方任务失败、超时或网络错误。

        """
        del policy
        if context.cancel_check is not None:
            context.cancel_check()
        with tempfile.NamedTemporaryFile(suffix=".pdf") as temporary:
            temporary.write(source.content)
            temporary.flush()
            with self._client() as client:
                result = self._call(client, Path(temporary.name))
                if context.cancel_check is not None:
                    context.cancel_check()
                jobs = _job_ids(result)
                raw_pages = official_pages(result)
                if len(raw_pages) == inspection.page_count:
                    pages = normalize_paddle_pages(raw_pages, inspection)
                else:
                    pages, recovery_jobs = self._recover_pages(
                        client,
                        Path(temporary.name),
                        inspection,
                        context,
                    )
                    jobs = tuple(dict.fromkeys((*jobs, *recovery_jobs)))
        if context.page_progress is not None:
            context.page_progress(len(pages), (), False)
        return PdfParseResult(
            source_sha256=inspection.source_sha256,
            parser_mode=PdfParserMode.OFFICIAL_API,
            parser_model=self.parser_model,
            parser_revision=self.parser_revision,
            parser_options_identity=self.parser_options_identity,
            page_count=inspection.page_count,
            pages=pages,
            provider_job_ids=jobs,
        )

    def close(self) -> None:
        """官方客户端按调用作用域关闭，无持久资源。"""

    @contextlib.contextmanager
    def _client(self) -> Iterator[object]:
        client = self._client_factory(
            self._config.access_token,
            self._config.request_timeout_seconds,
            self._config.poll_timeout_seconds,
        )
        entered = getattr(client, "__enter__", None)
        exited = getattr(client, "__exit__", None)
        if callable(entered) and callable(exited):
            scoped = entered()
            try:
                yield scoped
            finally:
                exited(None, None, None)
            return
        try:
            yield client
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()

    def _recover_pages(
        self,
        client: object,
        path: Path,
        inspection: PdfSourceInspection,
        context: ParseContext,
    ) -> tuple[tuple[PdfPage, ...], tuple[str, ...]]:
        pages: list[PdfPage] = []
        jobs: list[str] = []
        failed: list[int] = []
        for page_index in range(inspection.page_count):
            if context.cancel_check is not None:
                context.cancel_check()
            try:
                result = self._call(
                    client,
                    path,
                    page_ranges=str(page_index + 1),
                )
                raw_pages = official_pages(result)
                if len(raw_pages) != 1:
                    failed.append(page_index)
                    continue
                pages.extend(
                    normalize_paddle_pages(
                        raw_pages,
                        inspection,
                        page_indices=(page_index,),
                    )
                )
                job_id = provider_job_id(result)
                if job_id is not None:
                    jobs.append(job_id)
            except ProviderUnavailable:
                failed.append(page_index)
            if context.page_progress is not None:
                context.page_progress(len(pages), tuple(failed), False)
        if failed or len(pages) != inspection.page_count:
            if context.page_progress is not None:
                context.page_progress(len(pages), tuple(failed), True)
            raise ProviderInvalidResponse(
                "PaddleOCR 官方 API 未返回完整物理页面集合。",
                stage="pdf.paddle.official.pages",
                code="PDF_PAGE_COUNT_MISMATCH",
                details={
                    "expected_page_count": inspection.page_count,
                    "observed_page_count": len(pages),
                    "failed_page_indices": failed,
                },
            )
        return tuple(pages), tuple(dict.fromkeys(jobs))

    def _call(
        self,
        client: object,
        path: Path,
        *,
        page_ranges: str | None = None,
    ) -> object:
        parser = getattr(client, "parse_document", None)
        if not callable(parser):
            raise ConfigurationError(
                "PaddleOCR 官方 API Client 缺少 parse_document。",
                stage="pdf.paddle.official.configuration",
                code="PADDLE_OFFICIAL_CLIENT_INVALID",
            )
        kwargs = {
            "file_path": str(path),
            "model": _official_model(self.parser_model),
            "options": _official_options(self.parser_model),
        }
        if page_ranges is not None:
            kwargs["page_ranges"] = page_ranges
        try:
            return parser(**kwargs)
        except Exception as error:
            raise _map_client_error(error) from error


def _official_client(
    token: str, request_timeout: float, poll_timeout: float
) -> object:
    return OfficialPaddleOcrApiClient(
        token=token,
        request_timeout=request_timeout,
        poll_timeout=poll_timeout,
    )


def _official_model(model: str) -> str:
    return model


def _official_options(model: str) -> dict[str, object]:
    if model == "PP-StructureV3":
        return {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "prettify_markdown": False,
            "return_markdown_images": False,
            "visualize": False,
        }
    return {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_layout_detection": True,
        "use_chart_recognition": False,
        "prettify_markdown": False,
        "restructure_pages": False,
        "return_markdown_images": False,
        "visualize": False,
    }


def _map_client_error(error: Exception) -> Exception:
    name = type(error).__name__
    if name == "AuthError":
        return ProviderAuthenticationError(
            "PaddleOCR 官方 API 鉴权失败。",
            stage="pdf.paddle.official.api",
            code="PADDLE_AUTHENTICATION_FAILED",
        )
    if name == "RateLimitError":
        return ProviderRateLimited(
            "PaddleOCR 官方 API 触发限流。",
            stage="pdf.paddle.official.api",
            code="PADDLE_RATE_LIMITED",
        )
    code = {
        "PollTimeoutError": "PADDLE_POLL_TIMEOUT",
        "JobFailedError": "PADDLE_JOB_FAILED",
        "ResultParseError": "PADDLE_RESULT_PARSE_FAILED",
        "ResponseFormatError": "PADDLE_RESULT_PARSE_FAILED",
        "InvalidRequestError": "PADDLE_REQUEST_INVALID",
    }.get(name)
    if code is not None:
        return ProviderInvalidResponse(
            "PaddleOCR 官方 API 任务或结果无效。",
            stage="pdf.paddle.official.api",
            code=code,
            details={"error_type": name},
        )
    if name in _RETRYABLE_CLIENT_ERRORS:
        return ProviderUnavailable(
            "PaddleOCR 官方 API 暂时不可用。",
            stage="pdf.paddle.official.api",
            code="PADDLE_DOCUMENT_PARSE_UNAVAILABLE",
            details={"error_type": name},
        )
    return ProviderUnavailable(
        "PaddleOCR 官方 API 调用失败。",
        stage="pdf.paddle.official.api",
        code="PADDLE_DOCUMENT_PARSE_FAILED",
        details={"error_type": name},
        retryable=False,
    )


def _job_ids(result: object) -> tuple[str, ...]:
    job_id = provider_job_id(result)
    return () if job_id is None else (job_id,)


__all__ = ["PaddleOfficialApiPdfConfig", "PaddleOfficialApiPdfParser"]
