"""模板使用原件解析，并以版本化身份记录检索策略。"""

from __future__ import annotations

from rag_app.core.capabilities import ComponentDescriptor, ParserCapabilities
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import ParseContext, ParseResult, ParseSource
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import ParserPort


class WanshitongTemplateParser:
    """让模板填写指引与普通文档一样进入原件阅读视图。"""

    def __init__(self, fallback: ParserPort) -> None:
        self._fallback = fallback

    @property
    def descriptor(self) -> ComponentDescriptor:
        """把模板正文入索引的策略修订纳入 Parser 身份。"""
        fallback = self._fallback.descriptor
        identity = canonical_sha256(
            {
                "fallback": fallback.model_dump(mode="json"),
                "template": "original-docx-guidance-v3",
            }
        )
        return fallback.model_copy(
            update={
                "name": "wanshitong-template-source-v3",
                "version": f"3+{identity[-16:]}",
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
        """保留安全 Parser 产出的真实节点、原件摘要和来源跨度。"""
        return self._fallback.parse(source, policy, context)
