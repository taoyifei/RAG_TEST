"""自然问答的请求级来源范围；弱来源措辞不改变授权范围。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from rag_app.application.retrieval.source_scope import _resolve_mention
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.query_plan import (
    SourceDocumentIdentity,
    SourceResolution,
)
from rag_app.core.ports.evidence_source import CatalogDocument

NATURAL_SOURCE_SCOPE_REVISION = "wk-source-scope-cq1"
_QUOTED_MENTION = re.compile(r"《(?P<mention>[^《》\r\n]{1,160})》")
_UNQUOTED_PREFIX = re.compile(
    r"^\s*(?:请|请问|只)?\s*"
    r"(?P<trigger>根据|依据|依照|按照|在|从|仅查|只查|仅查询|只查询)"
    r"\s*(?P<mention>[^，,：:；;。！？?\r\n]{2,160}?"
    r"(?:规范|文档|制度|手册|办法|方案|指引|说明书|会议纪要|模板|清单|报告))"
    r"(?:中|里|内)?[，,：:\s]"
)
_SOFT = re.compile(
    r"^\s*(?P<mention>[^，,：:；;。！？?\r\n]{2,100}?"
    r"(?:规范|文档|制度|手册|办法|方案|指引|说明书|会议纪要|模板|清单|报告))"
    r"(?:中|里|内)(?:[，,：:\s]|(?=有哪些|有什么|是否|如何|怎么))"
)


@dataclass(frozen=True, slots=True)
class NaturalSourceScope:
    """在检索前冻结的来源范围决策。"""

    mode: Literal["OPEN", "SOFT_HINT", "HARD_RESOLVED", "HARD_UNRESOLVED"]
    trigger: str
    mentions: tuple[str, ...] = ()
    resolution: str = "OPEN"
    allowed_documents: tuple[SourceDocumentIdentity, ...] = ()
    soft_hint: str | None = None

    @property
    def is_hard(self) -> bool:
        """是否需要精确目录绑定。"""
        return self.mode.startswith("HARD_")

    @property
    def scope_digest(self) -> str:
        """将模式与已解析的版本身份绑定到脱敏 Trace。"""
        return canonical_sha256(
            {
                "revision": NATURAL_SOURCE_SCOPE_REVISION,
                "mode": self.mode,
                "trigger": self.trigger,
                "mentions": self.mentions,
                "resolution": self.resolution,
                "allowed": tuple(
                    (item.document_id, item.document_version_id)
                    for item in self.allowed_documents
                ),
            }
        )


def needs_natural_source_catalog(
    query: str,
    *,
    selected: bool = False,
    ignore_mentioned_sources: bool = False,
) -> bool:
    """书名号文档候选与结构化选文档均需要当前可见目录。"""
    if ignore_mentioned_sources and not selected:
        return False
    normalized = unicodedata.normalize("NFKC", query).strip()
    return bool(
        selected
        or _QUOTED_MENTION.search(normalized)
        or _UNQUOTED_PREFIX.match(normalized)
    )


def resolve_natural_source_scope(  # noqa: PLR0911
    query: str,
    documents: tuple[CatalogDocument, ...],
    *,
    selected_documents: tuple[SourceDocumentIdentity, ...] = (),
    ignore_mentioned_sources: bool = False,
) -> NaturalSourceScope:
    """从服务端选择或原问书名号签发精确文档版本许可。"""
    normalized = unicodedata.normalize("NFKC", query).strip()
    if selected_documents:
        visible = {
            (item.document_id, item.document_version_id) for item in documents
        }
        selected = {
            (item.document_id, item.document_version_id)
            for item in selected_documents
        }
        if selected <= visible:
            return NaturalSourceScope(
                "HARD_RESOLVED",
                "SERVER_SELECTION",
                resolution="RESOLVED",
                allowed_documents=selected_documents,
            )
        return NaturalSourceScope(
            "HARD_UNRESOLVED", "SERVER_SELECTION", resolution="UNRESOLVED"
        )
    if ignore_mentioned_sources:
        return NaturalSourceScope("OPEN", "USER_RELEASED")
    quoted = tuple(
        dict.fromkeys(
            match["mention"].strip()
            for match in _QUOTED_MENTION.finditer(normalized)
        )
    )
    if quoted:
        resolutions = tuple(
            _resolve_mention(mention, documents) for mention in quoted
        )
        unresolved = next(
            (
                resolution
                for resolution, _ in resolutions
                if resolution is not SourceResolution.RESOLVED
            ),
            None,
        )
        return NaturalSourceScope(
            "HARD_UNRESOLVED" if unresolved is not None else "HARD_RESOLVED",
            "QUOTED_DOCUMENT",
            quoted,
            unresolved.value if unresolved is not None else "RESOLVED",
            (
                tuple(
                    dict.fromkeys(
                        item for _, allowed in resolutions for item in allowed
                    )
                )
                if unresolved is None
                else ()
            ),
        )
    unquoted = _UNQUOTED_PREFIX.match(normalized)
    if unquoted is not None:
        mention = unquoted["mention"].strip()
        resolution, allowed = _resolve_mention(mention, documents)
        if resolution is SourceResolution.RESOLVED:
            return NaturalSourceScope(
                "HARD_RESOLVED",
                unquoted["trigger"],
                (mention,),
                resolution.value,
                allowed,
            )
        return NaturalSourceScope(
            "SOFT_HINT",
            "UNQUOTED_UNRESOLVED",
            (mention,),
            resolution.value,
            soft_hint=mention,
        )
    soft = _SOFT.match(normalized)
    if soft is not None:
        mention = soft["mention"].strip()
        return NaturalSourceScope(
            "SOFT_HINT",
            "SOURCE_PHRASE",
            (mention,),
            soft_hint=mention,
        )
    return NaturalSourceScope("OPEN", "NONE")


__all__ = [
    "NATURAL_SOURCE_SCOPE_REVISION",
    "NaturalSourceScope",
    "needs_natural_source_catalog",
    "resolve_natural_source_scope",
]
