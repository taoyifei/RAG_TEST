"""模板原件与检索目录提示分离，确保文档版本可按原件重建。"""

from __future__ import annotations

from rag_app.adapters.parsers.reading_view import build_reading_view_result
from rag_app.core.capabilities import ComponentDescriptor, ParserCapabilities
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import ParseContext, ParseResult, ParseSource
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import ParserPort
from rag_app.wanshitong.template_catalog import (
    is_template_source,
    template_index_markdown,
)


class WanshitongTemplateParser:
    """以用户原始 DOCX 作为 Source Artifact，派生目录提示用于检索。"""

    def __init__(self, fallback: ParserPort) -> None:
        self._fallback = fallback

    @property
    def descriptor(self) -> ComponentDescriptor:
        """把模板原件处理规则纳入 Parser 身份。"""
        fallback = self._fallback.descriptor
        identity = canonical_sha256(
            {"fallback": fallback.model_dump(mode="json"), "template": "v2"}
        )
        return fallback.model_copy(
            update={
                "name": "wanshitong-template-source-v2",
                "version": f"2+{identity[-16:]}",
            }
        )

    @property
    def parser_capabilities(self) -> ParserCapabilities:
        """普通文档与模板沿用同一格式能力声明。"""
        return self._fallback.parser_capabilities

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """先校验真实 DOCX，再建立绑定原件摘要的提示阅读视图。"""
        metadata = dict(context.document.metadata)
        relative_path = metadata.get("source_relative_path")
        if (
            source.extension != ".docx"
            or not isinstance(relative_path, str)
            or not is_template_source(relative_path)
        ):
            return self._fallback.parse(source, policy, context)
        # 重建时再次校验原始包；解析结果不包含模板正文的检索视图。
        self._fallback.parse(source, policy, context)
        title = metadata.get("document_title")
        safe_title = (
            title if isinstance(title, str) and title else source.display_name
        )
        return build_reading_view_result(
            source,
            context,
            policy,
            markdown=template_index_markdown(safe_title),
            parser_id="wanshitong-template-source",
            parser_version="2",
        )
