"""供应商无关的同步 PDF 文档解析端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor, ParserCapabilities
from rag_app.core.models import (
    ParseContext,
    ParseSource,
    PdfPageProgress,
    PdfParseResult,
    PdfParserMode,
    PdfSourceInspection,
)
from rag_app.core.policies import ParsingPolicy


class PdfDocumentParserPort(Protocol):
    """把已检查的 PDF 交给一种外部文档解析路径。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回包含模式、模型和实现版本的非敏感身份。"""

    @property
    def parser_capabilities(self) -> ParserCapabilities:
        """返回 PDF 结构支持能力。"""

    @property
    def parser_mode(self) -> PdfParserMode:
        """返回缓存隔离使用的执行模式。"""

    @property
    def parser_model(self) -> str:
        """返回实际文档解析模型。"""

    @property
    def parser_revision(self) -> str:
        """返回服务或 SDK 实现修订。"""

    @property
    def parser_options_identity(self) -> str:
        """返回不含 Secret 的完整解析选项身份。"""

    def parse_pdf(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
        inspection: PdfSourceInspection,
    ) -> PdfParseResult:
        """返回覆盖全部物理页的规范化结果。

        Args:
            source: 已验证签名、媒体类型与大小的 PDF 字节。
            policy: 当前冻结的资源边界。
            context: 当前逻辑文档和取消检查。
            inspection: 本地读取器证明的页数与逐页文本层状态。

        Returns:
            不含供应商原始响应的完整页集合。

        """

    def close(self) -> None:
        """释放 Adapter 持有的客户端资源。"""


class PdfParseCachePort(Protocol):
    """按完整解析身份读写已核验的规范化 PDF 结果。"""

    def get(
        self,
        *,
        source_sha256: str,
        parser_mode: str,
        parser_model: str,
        parser_revision: str,
        parser_options_identity: str,
    ) -> PdfParseResult | None:
        """返回完全匹配的缓存结果；未命中时返回 None。"""

    def put(self, result: PdfParseResult) -> None:
        """仅保存已经覆盖全部物理页的规范化结果。"""


class PdfProgressPort(Protocol):
    """保存不含 PDF 正文和 Provider 原始响应的 Job 进度。"""

    def put_progress(self, job_id: str, progress: PdfPageProgress) -> None:
        """覆盖保存一个 Job 的最新页面进度。"""

    def get_progress(self, job_id: str) -> PdfPageProgress | None:
        """返回 Job 的最新 PDF 进度；非 PDF Job 返回 None。"""


__all__ = ["PdfDocumentParserPort", "PdfParseCachePort", "PdfProgressPort"]
