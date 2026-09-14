"""按知识库配置选择 PaddleOCR PDF 解析路径。"""

from __future__ import annotations

import io
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from pypdf import PdfWriter

from rag_app.adapters.parsers.pdf import (
    PaddleOfficialApiPdfConfig,
    PaddleOfficialApiPdfParser,
    PaddleOfficialCriticalOcr,
    PaddleSelfHostedCriticalOcr,
    PaddleSelfHostedPdfConfig,
    PaddleSelfHostedPdfParser,
    PdfIrParser,
    inspect_pdf_source,
)
from rag_app.adapters.stores import (
    SqliteConnectionFactory,
    SqlitePdfParseCache,
    SqlitePdfProgressStore,
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
    ProviderRateLimited,
    ProviderUnavailable,
    RagError,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    DocumentRef,
    ParseContext,
    ParseResult,
    ParseSource,
)
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import (
    BlobStorePort,
    CriticalOcrVerifierPort,
    ParserPort,
    PdfDocumentParserPort,
)
from rag_app.product.catalog import CATALOG_VERSION, validate_model
from rag_app.product.control_store import ProductControlStore
from rag_app.product.credential_store import CredentialStore
from rag_app.product.model_settings import ProductModelSettings
from rag_app.product.models import ProviderConnection, ProviderValidationRun
from rag_app.product.pdf_ocr_verification import ProductCriticalOcrVerifier
from rag_app.product.verification import endpoint_identity

OfficialClientFactory = Callable[[str, float, float], object]
SelfHostedClientFactory = Callable[[ProviderConnection], httpx.Client]


class ProductPdfParsing:
    """把模型设置、连接、凭据、缓存和进度组装为 PDF Parser。"""

    def __init__(  # noqa: PLR0913
        self,
        connections: SqliteConnectionFactory,
        models: ProductModelSettings,
        control: ProductControlStore,
        credentials: CredentialStore,
        *,
        self_hosted_client_factory: SelfHostedClientFactory | None = None,
        official_client_factory: OfficialClientFactory | None = None,
    ) -> None:
        """保存产品控制面，不在构造时读取 Secret 或调用 Provider。

        Args:
            connections: 已完成 PDF migration 的 SQLite 连接工厂。
            models: 知识库模型与 PDF 解析设置。
            control: Provider Connection 控制面。
            credentials: 只在实际解析边界解密的凭据存储。
            self_hosted_client_factory: 合同测试可注入的 HTTP Client 工厂。
            official_client_factory: 合同测试可注入的官方 SDK Client 工厂。

        Returns:
            无返回值。

        """
        self._models = models
        self._connections = connections
        self._control = control
        self._credentials = credentials
        self._cache = SqlitePdfParseCache(connections)
        self._progress = SqlitePdfProgressStore(connections)
        self._self_hosted_client_factory = self_hosted_client_factory
        self._official_client_factory = official_client_factory
        self._blob_store: BlobStorePort | None = None

    def bind_blob_store(self, blob_store: BlobStorePort) -> None:
        """绑定只读原始 PDF Blob，用于最终高风险事实的小区域复核。

        Args:
            blob_store: P09 已拥有的 content-addressed Blob Store。

        Returns:
            无返回值。

        """
        if self._blob_store is not None:
            raise RuntimeError(
                "Product PDF 解析服务不允许重复绑定 Blob Store。"
            )
        self._blob_store = blob_store

    def critical_verifier(
        self, knowledge_base_id: str
    ) -> CriticalOcrVerifierPort:
        """返回一次 Query 独享的有界 PP-OCRv6 复核器。

        Args:
            knowledge_base_id: 当前 Query 的知识库。

        Returns:
            懒加载凭据、原 PDF 和 Provider 的复核端口。

        """
        return ProductCriticalOcrVerifier(
            lambda document_id, document_version_id: self._source_pdf(
                knowledge_base_id,
                document_id,
                document_version_id,
            ),
            lambda: self._critical_adapter(knowledge_base_id),
        )

    def parser(self, knowledge_base_id: str) -> PdfIrParser:
        """按知识库设置构造一次调用使用的 PDF Parser。

        Args:
            knowledge_base_id: PDF 所属知识库。

        Returns:
            已绑定本地或官方 PaddleOCR Adapter 的 IR Parser。

        Raises:
            ConfigurationError: PDF 解析未启用或连接配置不完整。

        """
        settings = self._models.get(knowledge_base_id)
        if (
            not settings.pdf_parser_enabled
            or settings.pdf_parser_connection_id is None
            or settings.pdf_parser_model is None
        ):
            raise ConfigurationError(
                "当前知识库尚未配置并启用 PDF 解析。",
                stage="pdf.product.configuration",
                code="PDF_PARSER_NOT_CONFIGURED",
            )
        connection = self._connection(settings.pdf_parser_connection_id)
        token, _ = self._credentials.resolve(connection.credential_id)
        provider = self._provider(
            connection,
            model=settings.pdf_parser_model,
            token=token,
            request_timeout=settings.pdf_request_timeout_seconds,
            poll_timeout=settings.pdf_poll_timeout_seconds,
        )
        return PdfIrParser(
            provider,
            cache=self._cache,
            progress=self._progress,
        )

    def validate_connection(
        self, connection_id: str, model: str
    ) -> ProviderValidationRun:
        """使用单页公开空白 PDF 验证真实 Paddle 文档解析合同。

        Args:
            connection_id: 待验证的 PaddleOCR 连接。
            model: 目录中的文档解析模型。

        Returns:
            与其它 Provider 共用的脱敏验证记录。

        """
        connection = self._connection(connection_id)
        validate_model(connection.provider_type, model, "document.parse")
        started = datetime.now(UTC)
        monotonic_start = time.monotonic()
        key_version = self._credentials.get(
            connection.credential_id
        ).key_version
        status = "succeeded"
        category = "live_200"
        safe_error: str | None = None
        request_dispatched = False
        provider_request_id: str | None = None
        validation_mode = (
            "mock"
            if self._self_hosted_client_factory is not None
            or self._official_client_factory is not None
            else "live"
        )
        provider: PdfDocumentParserPort | None = None
        try:
            token, key_version = self._credentials.resolve(
                connection.credential_id
            )
            provider = self._provider(
                connection,
                model=model,
                token=token,
                request_timeout=300.0,
                poll_timeout=600.0,
            )
            content = _synthetic_pdf()
            source = ParseSource(
                media_type="application/pdf",
                display_name="paddle-connection-check.pdf",
                content=content,
                extension=".pdf",
            )
            context = ParseContext(
                document=DocumentRef(
                    project_id="prj_" + "0" * 32,
                    knowledge_base_id="kb_" + "0" * 32,
                    document_id="doc_" + "0" * 32,
                    display_name=source.display_name,
                )
            )
            inspection = inspect_pdf_source(source, ParsingPolicy(), context)
            request_dispatched = True
            result = provider.parse_pdf(
                source, ParsingPolicy(), context, inspection
            )
            provider_request_id = next(iter(result.provider_job_ids), None)
        except RagError as error:
            status = "failed"
            safe_error = error.code
            if isinstance(error, ProviderAuthenticationError):
                category = "http_401"
            elif isinstance(error, ProviderRateLimited):
                category = "http_429"
            elif isinstance(error, ProviderUnavailable):
                category = "unavailable"
            else:
                category = "invalid_contract"
        finally:
            if provider is not None:
                provider.close()
        identity = endpoint_identity(connection)
        validation = ProviderValidationRun(
            validation_id="val_" + secrets.token_hex(16),
            connection_id=connection.connection_id,
            catalog_version=CATALOG_VERSION,
            operation="document.parse",
            provider_model=model,
            credential_key_version=key_version,
            request_policy_identity=canonical_sha256(
                {
                    "model": model,
                    "operation": "document.parse",
                    "options": "paddle-document-parse-v1",
                }
            ),
            started_at=started.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            status=status,
            http_category=(
                "mock_200"
                if validation_mode == "mock" and status == "succeeded"
                else category
            ),
            dimension=None,
            estimated_tokens=0,
            observed_tokens=None,
            latency_ms=max(0, int((time.monotonic() - monotonic_start) * 1000)),
            safe_error_code=safe_error,
            configuration_version=connection.configuration_version,
            stage="document_parse_contract",
            request_dispatched=request_dispatched,
            provider_request_id=provider_request_id,
            endpoint_mode=connection.endpoint_mode,
            endpoint_host=(
                None
                if connection.api_base_url is None
                else urlsplit(connection.api_base_url).hostname
            ),
            synthetic_payload_hash=canonical_sha256(
                {"fixture": "blank-pdf-v1", "page_count": 1}
            ),
            endpoint_identity=identity,
            validation_mode=validation_mode,
        )
        return self._control.record_validation(validation)

    def _provider(
        self,
        connection: ProviderConnection,
        *,
        model: str,
        token: str,
        request_timeout: float,
        poll_timeout: float,
    ) -> PdfDocumentParserPort:
        """构造绑定当前连接版本、但不泄漏 Secret 的 Adapter。"""
        provider: PdfDocumentParserPort
        if connection.endpoint_mode == "self_hosted":
            if connection.api_base_url is None:
                raise ConfigurationError(
                    "自托管 PaddleOCR 缺少 Base URL。",
                    stage="pdf.product.configuration",
                    code="PDF_PARSER_ENDPOINT_MISSING",
                )
            client = (
                None
                if self._self_hosted_client_factory is None
                else self._self_hosted_client_factory(connection)
            )
            provider = PaddleSelfHostedPdfParser(
                PaddleSelfHostedPdfConfig(
                    base_url=connection.api_base_url,
                    api_key=token,
                    model=model,
                    parser_revision=(
                        "paddleocr-3.7.0-pp-structure-v3"
                        if model == "PP-StructureV3"
                        else "paddleocr-3.7.0-pipeline-v1.6"
                    ),
                    request_timeout_seconds=request_timeout,
                ),
                client=client,
            )
        elif connection.endpoint_mode == "official_api":
            if not token:
                raise ConfigurationError(
                    "PaddleOCR 官方 API 必须配置 Access Token。",
                    stage="pdf.product.configuration",
                    code="PDF_PARSER_CREDENTIAL_MISSING",
                )
            provider = PaddleOfficialApiPdfParser(
                PaddleOfficialApiPdfConfig(
                    access_token=token,
                    model=model,
                    parser_revision="paddleocr-sdk-3.7.0",
                    request_timeout_seconds=request_timeout,
                    poll_timeout_seconds=poll_timeout,
                ),
                client_factory=self._official_client_factory,
            )
        else:
            raise ConfigurationError(
                "PaddleOCR 连接模式无效。",
                stage="pdf.product.configuration",
                code="PDF_PARSER_MODE_INVALID",
            )
        return provider

    def _critical_adapter(
        self, knowledge_base_id: str
    ) -> PaddleSelfHostedCriticalOcr | PaddleOfficialCriticalOcr:
        """使用当前 PDF 连接构造 PP-OCRv6 区域复核 Adapter。"""
        settings = self._models.get(knowledge_base_id)
        if (
            not settings.pdf_parser_enabled
            or settings.pdf_parser_connection_id is None
        ):
            raise ConfigurationError(
                "当前知识库没有可用于高风险事实复核的 PaddleOCR 连接。",
                stage="answer.ocr_verify.configuration",
                code="OCR_VERIFICATION_NOT_CONFIGURED",
            )
        connection = self._connection(settings.pdf_parser_connection_id)
        token, _ = self._credentials.resolve(connection.credential_id)
        if connection.endpoint_mode == "self_hosted":
            if connection.ocr_api_base_url is None:
                raise ConfigurationError(
                    "自托管 PaddleOCR 未配置 PP-OCRv6 复核 Base URL。",
                    stage="answer.ocr_verify.configuration",
                    code="OCR_VERIFICATION_ENDPOINT_MISSING",
                )
            client = (
                None
                if self._self_hosted_client_factory is None
                else self._self_hosted_client_factory(connection)
            )
            return PaddleSelfHostedCriticalOcr(
                provider_id=connection.connection_id,
                base_url=connection.ocr_api_base_url,
                api_key=token,
                timeout_seconds=settings.pdf_request_timeout_seconds,
                client=client,
            )
        if connection.endpoint_mode == "official_api":
            if not token:
                raise ConfigurationError(
                    "PaddleOCR 官方 API 必须配置 Access Token。",
                    stage="answer.ocr_verify.configuration",
                    code="OCR_VERIFICATION_CREDENTIAL_MISSING",
                )
            return PaddleOfficialCriticalOcr(
                provider_id=connection.connection_id,
                access_token=token,
                request_timeout_seconds=settings.pdf_request_timeout_seconds,
                poll_timeout_seconds=settings.pdf_poll_timeout_seconds,
                client_factory=self._official_client_factory,
            )
        raise ConfigurationError(
            "PaddleOCR 连接模式无效。",
            stage="answer.ocr_verify.configuration",
            code="OCR_VERIFICATION_MODE_INVALID",
        )

    def _source_pdf(
        self,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> bytes:
        """按知识库、逻辑文档和不可变版本回读原始 PDF。"""
        if self._blob_store is None:
            raise RuntimeError("Product PDF 解析服务尚未绑定 Blob Store。")
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT dv.source_artifact_id,dv.media_type "
                "FROM document_versions dv "
                "JOIN documents d ON d.document_id=dv.document_id "
                "JOIN knowledge_bases kb "
                "ON kb.knowledge_base_id=d.knowledge_base_id "
                "JOIN projects p ON p.project_id=d.project_id "
                "WHERE d.knowledge_base_id=? AND d.document_id=? "
                "AND dv.document_version_id=? "
                "AND d.status='active' AND d.deleted_at IS NULL "
                "AND kb.deleted_at IS NULL AND p.deleted_at IS NULL",
                (knowledge_base_id, document_id, document_version_id),
            ).fetchone()
        if row is None or str(row["media_type"]) != "application/pdf":
            raise LookupError("当前文档版本不是可读取的 PDF。")
        blob = self._blob_store.read(str(row["source_artifact_id"]))
        if blob is None or blob.media_type != "application/pdf":
            raise LookupError("当前 PDF 版本的原始 Artifact 不可读取。")
        return blob.content

    def content_identity(self, knowledge_base_id: str) -> str | None:
        """返回会改变 PDF 派生正文的非敏感身份。

        Args:
            knowledge_base_id: 当前知识库。

        Returns:
            未启用时为 None；否则绑定模式、模型、版本和解析选项。

        """
        settings = self._models.get(knowledge_base_id)
        if not settings.pdf_parser_enabled:
            return None
        if (
            settings.pdf_parser_connection_id is None
            or settings.pdf_parser_model is None
        ):
            return canonical_sha256({"state": "invalid"})
        connection = self._connection(settings.pdf_parser_connection_id)
        return canonical_sha256(
            {
                "provider": "paddleocr",
                "mode": connection.endpoint_mode,
                "model": settings.pdf_parser_model,
                "implementation": "paddleocr-3.7.0/pdf-ir-v1",
                "endpoint_identity": (
                    None
                    if connection.api_base_url is None
                    else canonical_sha256(connection.api_base_url)
                ),
                "options": {
                    "use_layout_detection": True,
                    "prettify_markdown": False,
                    "restructure_pages": False,
                    "return_markdown_images": False,
                    "visualize": False,
                },
            }
        )

    def verification_identity(self, knowledge_base_id: str) -> str | None:
        """返回只影响答案复核与查询缓存、不触发 PDF 重解析的身份。"""
        settings = self._models.get(knowledge_base_id)
        if (
            not settings.pdf_parser_enabled
            or settings.pdf_parser_connection_id is None
        ):
            return None
        try:
            connection = self._connection(settings.pdf_parser_connection_id)
        except RagError:
            return canonical_sha256({"state": "invalid"})
        return canonical_sha256(
            {
                "provider": "paddleocr",
                "mode": connection.endpoint_mode,
                "credential_version": self._control.credential_version(
                    connection.credential_id
                ),
                "endpoint_identity": (
                    None
                    if connection.ocr_api_base_url is None
                    else canonical_sha256(connection.ocr_api_base_url)
                ),
                "model": "PP-OCRv6",
                "region": "pdf-bbox-render-v1",
                "critical_atom_policy": "ocr-critical-atoms-v1",
            }
        )

    def wrap(self, fallback: ParserPort) -> ProductDocumentParser:
        """返回 Word/PDF 共用的格式路由 Parser。

        Args:
            fallback: 当前稳定 Word Parser。

        Returns:
            仅对 `.pdf` 选择 PaddleOCR 的 ParserPort。

        """
        return ProductDocumentParser(fallback, self)

    def _connection(self, connection_id: str) -> ProviderConnection:
        connection = self._control.get_connection(connection_id)
        if connection.provider_type != "paddleocr" or not connection.enabled:
            raise ConfigurationError(
                "PDF 解析必须引用已启用的 PaddleOCR 连接。",
                stage="pdf.product.configuration",
                code="PDF_PARSER_CONNECTION_INVALID",
            )
        return connection


class ProductDocumentParser:
    """保留 Word Parser，仅把 PDF 路由给当前知识库的 PaddleOCR。"""

    parser_capabilities = ParserCapabilities(
        supported_extensions=(".doc", ".docx", ".pdf"),
        supported_media_types=(
            "application/msword",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            "application/pdf",
        ),
        supports_tables=True,
        supports_images="partial",
        supports_numbering="partial",
        supports_headers_footers="partial",
        supports_footnotes="partial",
        supports_revisions="partial",
        supports_comments="partial",
        supports_text_boxes="partial",
    )

    def __init__(self, fallback: ParserPort, pdf: ProductPdfParsing) -> None:
        self._fallback = fallback
        self._pdf = pdf

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回稳定格式路由身份；具体 Paddle 身份进入内容指纹。"""
        return ComponentDescriptor(
            kind=ComponentKind.PARSER,
            name="word-paddle-document",
            version="1",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                permits_network=True,
                formats=(
                    "application/msword",
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document",
                    "application/pdf",
                ),
            ),
        )

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """按扩展名选择 Word 或 PDF，后续统一返回 DocumentIR。

        Args:
            source: 已由生命周期签名检查的文档字节。
            policy: 当前冻结解析策略。
            context: 文档、Job、Revision 和取消边界。

        Returns:
            当前格式 Parser 的统一 ParseResult。

        """
        if source.extension.casefold() != ".pdf":
            return self._fallback.parse(source, policy, context)
        parser = self._pdf.parser(context.document.knowledge_base_id)
        try:
            return parser.parse(source, policy, context)
        finally:
            parser.close()


def _synthetic_pdf() -> bytes:
    """生成不含用户资料的最小单页 PDF 连接探针。"""
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


__all__ = ["ProductDocumentParser", "ProductPdfParsing"]
