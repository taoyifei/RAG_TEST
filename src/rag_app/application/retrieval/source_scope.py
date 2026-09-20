"""把原问题中的明确来源解析为逐 Atom 文档身份许可。"""

from __future__ import annotations

import re
import unicodedata

from rag_app.application.retrieval.semantics import split_explicit_source_scope
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.query_plan import (
    QUERY_PLAN_SCHEMA_REVISION,
    SOURCE_SCOPE_SCHEMA_REVISION,
    QueryAtom,
    QueryPlan,
    SourceContentRequirement,
    SourceDocumentIdentity,
    SourceIntent,
    SourceResolution,
    SourceScopeDecision,
)
from rag_app.core.ports.evidence_source import CatalogDocument
from rag_app.core.source_scope import (
    evidence_allowed_for_atom,
    filter_identities_for_scope,
    source_identity_allowed,
)

_LEADING_BOOK_AUTHORITY = re.compile(
    r"^\s*《(?P<source>[^》]{2,160})》\s*"
    r"(?:的|中|里|规定|要求|所述|指出)"
)
_REFERENCE_OWNER = re.compile(
    r"^\s*(?P<source>[^，。！？?《》]{2,80}?"
    r"(?:文档|规范|制度|手册|方案|模板|指引))"
    r"(?:中|里)?(?:有没有|是否|可否)?(?:提到|提及|引用|列出)"
)
_BOOK_TITLE = re.compile(r"《(?P<title>[^》]{2,160})》")
_COMPARISON = re.compile(r"比较|对比|差异|区别|不同")
_EXISTENCE = re.compile(
    r"是否(?:存在|入库|收录)|有没有(?:这|该|此)?(?:份|个)?"
    r"(?:文档|文件|模板|规范|制度|手册)|目录(?:中|里)?(?:有|列出)"
)
_REFERENCE = re.compile(r"提到|提及|引用|参考对象|列为参考")
_DOCUMENT_EXTENSION = re.compile(r"(?i)\.(?:docx?|pdf|xlsx?|pptx?|txt|md)$")
_TRUSTED_ALIAS_KEYS = (
    "trusted_aliases",
    "document_aliases",
    "source_aliases",
    "aliases",
)
_CATALOG_ONLY_KEYS = (
    "catalog_only",
    "body_available",
    "content_available",
)
_MIN_COMPARISON_DOCUMENTS = 2


def normalize_source_label(value: str) -> str:
    """规范化用于身份精确匹配的显示标签，不删除版本或主体差异。

    Args:
        value: 文档标题、路径末段或受信别名。

    Returns:
        去除显示包装、扩展名和空白后的 NFKC 标签。

    """
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = normalized.strip("《》〈〉\"'“”‘’ ")
    normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    normalized = _DOCUMENT_EXTENSION.sub("", normalized).strip()
    return "".join(normalized.casefold().split())


def _document_labels(document: CatalogDocument) -> frozenset[str]:
    metadata = dict(document.metadata)
    labels: set[str] = {document.title}
    for key in ("document_title", "display_name", "source_relative_path"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            labels.add(value)
    for key in _TRUSTED_ALIAS_KEYS:
        values = metadata.get(key)
        if isinstance(values, str):
            labels.add(values)
        elif isinstance(values, (tuple, list)):
            labels.update(
                item
                for item in values
                if isinstance(item, str) and item.strip()
            )
    return frozenset(
        normalized
        for value in labels
        if (normalized := normalize_source_label(value))
    )


def _catalog_only(document: CatalogDocument) -> bool:
    metadata = dict(document.metadata)
    for key in _CATALOG_ONLY_KEYS:
        value = metadata.get(key)
        if key == "catalog_only" and value is True:
            return True
        if key != "catalog_only" and value is False:
            return True
    return False


def _source_mentions(query: str) -> tuple[SourceIntent, tuple[str, ...]]:
    normalized = unicodedata.normalize("NFKC", query).strip()
    source, _body, _offset = split_explicit_source_scope(normalized)
    if source:
        return SourceIntent.DOCUMENT_AUTHORITY, (source,)
    leading = _LEADING_BOOK_AUTHORITY.match(normalized)
    if leading is not None:
        return SourceIntent.DOCUMENT_AUTHORITY, (leading["source"],)
    owner = _REFERENCE_OWNER.match(normalized)
    if owner is not None:
        return SourceIntent.MENTION_IN_SOURCE, (owner["source"],)
    book_titles = tuple(
        dict.fromkeys(
            match["title"].strip() for match in _BOOK_TITLE.finditer(normalized)
        )
    )
    if len(book_titles) >= _MIN_COMPARISON_DOCUMENTS and _COMPARISON.search(
        normalized
    ):
        return SourceIntent.DOCUMENT_SET, book_titles
    return SourceIntent.OPEN, ()


def _required_content(
    query: str, source_intent: SourceIntent
) -> SourceContentRequirement:
    if source_intent is SourceIntent.MENTION_IN_SOURCE or _REFERENCE.search(
        query
    ):
        return SourceContentRequirement.REFERENCE
    if _EXISTENCE.search(query):
        return SourceContentRequirement.EXISTENCE
    return SourceContentRequirement.BODY


def _decision(  # noqa: PLR0913
    *,
    atom_id: str,
    source_intent: SourceIntent,
    resolution: SourceResolution,
    allowed_documents: tuple[SourceDocumentIdentity, ...],
    required_content: SourceContentRequirement,
    mention: str | None,
    registry_revision: str,
) -> SourceScopeDecision:
    mention_sha256 = canonical_sha256(mention) if mention is not None else None
    digest = canonical_sha256(
        {
            "schema": SOURCE_SCOPE_SCHEMA_REVISION,
            "atom_id": atom_id,
            "source_intent": source_intent.value,
            "resolution": resolution.value,
            "allowed_documents": [
                item.model_dump(mode="json") for item in allowed_documents
            ],
            "required_content": required_content.value,
            "mention_sha256": mention_sha256,
            "registry_revision": registry_revision,
        }
    )
    return SourceScopeDecision(
        atom_id=atom_id,
        source_intent=source_intent,
        resolution=resolution,
        allowed_documents=allowed_documents,
        required_content=required_content,
        mention_sha256=mention_sha256,
        registry_revision=registry_revision,
        scope_digest=digest,
    )


def _resolve_mention(
    mention: str,
    documents: tuple[CatalogDocument, ...],
) -> tuple[SourceResolution, tuple[SourceDocumentIdentity, ...]]:
    target = normalize_source_label(mention)
    matches = tuple(
        document
        for document in documents
        if target in _document_labels(document)
    )
    if not matches:
        return SourceResolution.UNRESOLVED, ()
    identities = tuple(
        SourceDocumentIdentity(
            document_id=document.document_id,
            document_version_id=document.document_version_id,
        )
        for document in sorted(
            matches,
            key=lambda item: (item.document_id, item.document_version_id),
        )
    )
    if len(identities) != 1:
        return SourceResolution.AMBIGUOUS, ()
    if _catalog_only(matches[0]):
        return SourceResolution.CATALOG_ONLY, ()
    return SourceResolution.RESOLVED, identities


def resolve_query_plan_source_scopes(
    query_plan: QueryPlan,
    documents: tuple[CatalogDocument, ...],
    *,
    registry_revision: str,
) -> QueryPlan:
    """为 QueryPlan 的每个 Atom 签发精确文档身份许可。

    Planner 的来源字段可以补充原问，但原问中已明确的来源不会因为
    Planner 漏字段而退回开放查询。所有匹配均基于活动目录的精确标签或
    受信别名，不使用向量排名或标题子串。

    Args:
        query_plan: 已完成原子拆分的当前查询计划。
        documents: 当前请求可见的完整活动文档目录。
        registry_revision: 当前目录与解析策略的稳定修订身份。

    Returns:
        携带逐 Atom ``SourceScopeDecision`` 且重新签名的 QueryPlan。

    """
    root_intent, root_mentions = _source_mentions(query_plan.original_query)
    content = _required_content(query_plan.original_query, root_intent)
    scoped_atoms: list[QueryAtom] = []
    for atom in query_plan.atoms:
        intent = root_intent
        mentions = root_mentions
        if not mentions and atom.source_qualifier:
            intent = SourceIntent.DOCUMENT_AUTHORITY
            mentions = (atom.source_qualifier,)
        if not mentions:
            scope = _decision(
                atom_id=atom.atom_id,
                source_intent=SourceIntent.OPEN,
                resolution=SourceResolution.OPEN,
                allowed_documents=(),
                required_content=SourceContentRequirement.BODY,
                mention=None,
                registry_revision=registry_revision,
            )
        elif intent is SourceIntent.DOCUMENT_SET:
            resolved = tuple(
                _resolve_mention(mention, documents) for mention in mentions
            )
            if all(
                state is SourceResolution.RESOLVED for state, _ids in resolved
            ):
                allowed = tuple(
                    dict.fromkeys(
                        identity
                        for _state, identities in resolved
                        for identity in identities
                    )
                )
                resolution = SourceResolution.RESOLVED
            else:
                allowed = ()
                resolution = (
                    SourceResolution.AMBIGUOUS
                    if any(
                        state is SourceResolution.AMBIGUOUS
                        for state, _ids in resolved
                    )
                    else SourceResolution.UNRESOLVED
                )
            scope = _decision(
                atom_id=atom.atom_id,
                source_intent=intent,
                resolution=resolution,
                allowed_documents=allowed,
                required_content=content,
                mention="\n".join(mentions),
                registry_revision=registry_revision,
            )
        else:
            resolution, allowed = _resolve_mention(mentions[0], documents)
            scope = _decision(
                atom_id=atom.atom_id,
                source_intent=intent,
                resolution=resolution,
                allowed_documents=allowed,
                required_content=content,
                mention=mentions[0],
                registry_revision=registry_revision,
            )
        scoped_atoms.append(atom.model_copy(update={"source_scope": scope}))
    scopes = tuple(atom.source_scope for atom in scoped_atoms)
    identity = canonical_sha256(
        {
            "schema": QUERY_PLAN_SCHEMA_REVISION,
            "base_plan_id": query_plan.plan_id,
            "source_scope_digests": [
                scope.scope_digest for scope in scopes if scope is not None
            ],
        }
    )
    return query_plan.model_copy(
        update={"plan_id": identity, "atoms": tuple(scoped_atoms)}
    )


def query_plan_requires_source_resolution(query_plan: QueryPlan) -> bool:
    """判断本次原问或 Atom 是否携带明确来源名称。"""
    _intent, mentions = _source_mentions(query_plan.original_query)
    return bool(
        mentions or any(atom.source_qualifier for atom in query_plan.atoms)
    )


__all__ = [
    "evidence_allowed_for_atom",
    "filter_identities_for_scope",
    "normalize_source_label",
    "query_plan_requires_source_resolution",
    "resolve_query_plan_source_scopes",
    "source_identity_allowed",
]
