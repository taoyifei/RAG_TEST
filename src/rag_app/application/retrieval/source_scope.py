"""把原问题中的明确来源解析为逐 Atom 文档身份许可。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rag_app.application.retrieval.semantics import split_explicit_source_scope
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.answer_plan import (
    NormalizedOffsetSpan,
    ResolvedQueryView,
    SourceMentionRole,
    SourceMentionSpan,
)
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
    r"^\s*(?P<source>[^，。！？?《》]{1,80}?"
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
_DOCUMENT_LOOKUP = re.compile(
    r"^(?:在哪|在哪里|哪里|哪找|是否(?:存在|入库|收录)|"
    r"有没有(?:这|该|此)?(?:份|个)?(?:文档|文件|模板|规范|制度|手册))"
    r"[？?]?$"
)
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
_MAX_QUERY_ATOMS = 4
_SOURCE_SCOPE_KEYS = tuple(f"SOURCE_{letter}" for letter in "ABCDEFGH")


@dataclass(frozen=True, slots=True)
class ResolvedSourceMention:
    """一处来源提及及其独立的文档身份许可。"""

    scope_key: str
    text: str
    resolution: SourceResolution
    allowed_documents: tuple[SourceDocumentIdentity, ...]
    scope_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedSourceContext:
    """业务分析前冻结的来源角色、身份和问题视图。"""

    query_view: ResolvedQueryView
    source_intent: SourceIntent
    mentions: tuple[str, ...]
    mention_scopes: tuple[ResolvedSourceMention, ...]
    resolution: SourceResolution
    allowed_documents: tuple[SourceDocumentIdentity, ...]
    required_content: SourceContentRequirement
    registry_revision: str
    scope_digest: str

    @property
    def resolution_required(self) -> bool:
        """返回原问是否声明了必须解析的来源权限。"""
        return bool(self.mentions)


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


def _normalized_offsets(value: str) -> tuple[NormalizedOffsetSpan, ...]:
    """保留 NFKC 文本边界到每个原始字符的可逆定位信息。"""
    prefix_lengths = tuple(
        len(unicodedata.normalize("NFKC", value[:index]))
        for index in range(len(value) + 1)
    )
    return tuple(
        NormalizedOffsetSpan(
            original_start=index,
            original_end=index + 1,
            normalized_start=prefix_lengths[index],
            normalized_end=prefix_lengths[index + 1],
        )
        for index in range(len(value))
    )


def _original_span(
    original: str,
    normalized: str,
    offsets: tuple[NormalizedOffsetSpan, ...],
    mention: str,
) -> tuple[int, int] | None:
    """在原文中定位来源标签，兼容全角和兼容字符。"""
    direct = original.find(mention)
    if direct >= 0:
        return direct, direct + len(mention)
    normalized_mention = unicodedata.normalize("NFKC", mention)
    normalized_start = normalized.find(normalized_mention)
    if normalized_start < 0:
        return None
    normalized_end = normalized_start + len(normalized_mention)
    covered = tuple(
        item
        for item in offsets
        if item.normalized_end > normalized_start
        and item.normalized_start < normalized_end
    )
    if not covered:
        return None
    return covered[0].original_start, covered[-1].original_end


def _mention_scope_digest(query_plan: QueryPlan) -> str | None:
    """读取当前请求已签发的来源范围摘要，不重新解析身份。"""
    return next(
        (
            atom.source_scope.scope_digest
            for atom in query_plan.atoms
            if atom.source_scope is not None
            and atom.source_scope.resolution is not SourceResolution.OPEN
        ),
        None,
    )


def _document_set_business_query(
    normalized: str,
    mentions: tuple[str, ...],
) -> str:
    """用不含标题词义的稳定来源键替换文档集合提及。"""
    mention_indexes = {
        unicodedata.normalize("NFKC", mention).strip(): index
        for index, mention in enumerate(mentions)
        if index < len(_SOURCE_SCOPE_KEYS)
    }
    replacements: list[tuple[int, int, str]] = []
    for match in _BOOK_TITLE.finditer(normalized):
        index = mention_indexes.get(match["title"].strip())
        if index is None:
            continue
        replacements.append(
            (match.start(), match.end(), f"【{_SOURCE_SCOPE_KEYS[index]}】")
        )
    result = normalized
    for start, end, replacement in reversed(replacements):
        result = f"{result[:start]}{replacement}{result[end:]}"
    return result


def _build_query_view(  # noqa: PLR0913
    original: str,
    *,
    intent: SourceIntent,
    mentions: tuple[str, ...],
    scope_digest: str | None,
    mention_scope_digests: tuple[str | None, ...] = (),
    fallback_qualifier: str | None = None,
) -> ResolvedQueryView:
    """只按已识别角色移除来源包装，保留所有原始跨度。"""
    normalized = unicodedata.normalize("NFKC", original)
    offsets = _normalized_offsets(original)
    leading_source, leading_body, _body_start = split_explicit_source_scope(
        normalized
    )
    if intent is SourceIntent.DOCUMENT_SET:
        business_query = _document_set_business_query(normalized, mentions)
    elif leading_source and intent is SourceIntent.DOCUMENT_AUTHORITY:
        business_query = leading_body
    else:
        business_query = normalized
    mention_specs: list[
        tuple[str, SourceMentionRole, str | None, str | None]
    ] = []
    if intent is SourceIntent.MENTION_IN_SOURCE and mentions:
        owner_match = _REFERENCE_OWNER.match(normalized)
        if owner_match is not None:
            business_query = normalized[owner_match.end("source") :].lstrip(
                " 的中里，,:："
            )
        mention_specs.append(
            (
                mentions[0],
                SourceMentionRole.SOURCE_OWNER,
                _SOURCE_SCOPE_KEYS[0],
                (
                    mention_scope_digests[0]
                    if mention_scope_digests
                    else scope_digest
                ),
            )
        )
        mention_specs.extend(
            (
                match["title"].strip(),
                SourceMentionRole.REFERENCED_OBJECT,
                None,
                None,
            )
            for match in _BOOK_TITLE.finditer(normalized)
        )
    elif intent in {SourceIntent.DOCUMENT_AUTHORITY, SourceIntent.DOCUMENT_SET}:
        mention_specs.extend(
            (
                mention,
                SourceMentionRole.AUTHORITY,
                _SOURCE_SCOPE_KEYS[index],
                (
                    mention_scope_digests[index]
                    if index < len(mention_scope_digests)
                    else scope_digest
                ),
            )
            for index, mention in enumerate(mentions)
            if index < len(_SOURCE_SCOPE_KEYS)
        )
    else:
        mention_specs.extend(
            (
                match["title"].strip(),
                SourceMentionRole.REFERENCED_OBJECT,
                None,
                None,
            )
            for match in _BOOK_TITLE.finditer(normalized)
        )
        if not mention_specs and fallback_qualifier:
            mention_specs.append(
                (
                    fallback_qualifier,
                    SourceMentionRole.AUTHORITY,
                    _SOURCE_SCOPE_KEYS[0],
                    scope_digest,
                )
            )

    source_mentions: list[SourceMentionSpan] = []
    seen: set[tuple[int, int, SourceMentionRole]] = set()
    for mention, role, scope_key, mention_digest in mention_specs:
        span = _original_span(
            original,
            normalized,
            offsets,
            mention,
        )
        if span is None or (*span, role) in seen:
            continue
        seen.add((*span, role))
        start, end = span
        source_mentions.append(
            SourceMentionSpan(
                text=original[start:end],
                role=role,
                original_start=start,
                original_end=end,
                scope_key=scope_key,
                scope_digest=mention_digest,
            )
        )
    return ResolvedQueryView(
        original_query=original,
        normalized_query=normalized,
        business_query=business_query.strip() or normalized.strip(),
        normalized_offsets=offsets,
        source_mentions=tuple(
            sorted(
                source_mentions,
                key=lambda item: (item.original_start, item.original_end),
            )
        ),
    )


def build_resolved_query_view(query_plan: QueryPlan) -> ResolvedQueryView:
    """从已签发 QueryPlan 重建与前置阶段相同的问题视图。

    Args:
        query_plan: 已完成活动文档身份解析的兼容查询计划。

    Returns:
        保留原文定位且只移除来源包装的统一问题视图。

    """
    intent, mentions = _source_mentions(query_plan.original_query)
    scope_digest = _mention_scope_digest(query_plan)
    qualifier = next(
        (
            atom.source_qualifier
            for atom in query_plan.atoms
            if atom.source_qualifier
        ),
        None,
    )
    return _build_query_view(
        query_plan.original_query,
        intent=intent,
        mentions=mentions,
        scope_digest=scope_digest,
        fallback_qualifier=qualifier,
    )


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


def _source_mentions(  # noqa: PLR0911
    query: str,
) -> tuple[SourceIntent, tuple[str, ...]]:
    normalized = unicodedata.normalize("NFKC", query).strip()
    book_titles = tuple(
        dict.fromkeys(
            match["title"].strip() for match in _BOOK_TITLE.finditer(normalized)
        )
    )
    source, body, _offset = split_explicit_source_scope(normalized)
    if source:
        if _DOCUMENT_LOOKUP.fullmatch(body.strip()):
            return SourceIntent.OPEN, ()
        body_titles = tuple(_BOOK_TITLE.finditer(body))
        if (
            len(book_titles) >= _MIN_COMPARISON_DOCUMENTS
            and len(body_titles) == 1
            and _COMPARISON.search(normalized)
        ):
            return SourceIntent.DOCUMENT_SET, book_titles
        return SourceIntent.DOCUMENT_AUTHORITY, (source,)
    owner = _REFERENCE_OWNER.match(normalized)
    if owner is not None:
        return SourceIntent.MENTION_IN_SOURCE, (owner["source"],)
    if len(book_titles) >= _MIN_COMPARISON_DOCUMENTS and _COMPARISON.search(
        normalized
    ):
        return SourceIntent.DOCUMENT_SET, book_titles
    leading = _LEADING_BOOK_AUTHORITY.match(normalized)
    if leading is not None:
        return SourceIntent.DOCUMENT_AUTHORITY, (leading["source"],)
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


def _resolve_mentions(
    intent: SourceIntent,
    mentions: tuple[str, ...],
    documents: tuple[CatalogDocument, ...],
) -> tuple[SourceResolution, tuple[SourceDocumentIdentity, ...]]:
    """把请求级来源集合解析为不可跨文档借用的身份集合。"""
    if not mentions:
        return SourceResolution.OPEN, ()
    if intent is not SourceIntent.DOCUMENT_SET:
        return _resolve_mention(mentions[0], documents)
    resolved = tuple(
        _resolve_mention(mention, documents) for mention in mentions
    )
    if all(state is SourceResolution.RESOLVED for state, _ids in resolved):
        return (
            SourceResolution.RESOLVED,
            tuple(
                dict.fromkeys(
                    identity
                    for _state, identities in resolved
                    for identity in identities
                )
            ),
        )
    if any(state is SourceResolution.AMBIGUOUS for state, _ids in resolved):
        return SourceResolution.AMBIGUOUS, ()
    if any(state is SourceResolution.CATALOG_ONLY for state, _ids in resolved):
        return SourceResolution.CATALOG_ONLY, ()
    return SourceResolution.UNRESOLVED, ()


def _resolved_source_mentions(
    intent: SourceIntent,
    mentions: tuple[str, ...],
    documents: tuple[CatalogDocument, ...],
    *,
    required_content: SourceContentRequirement,
    registry_revision: str,
) -> tuple[ResolvedSourceMention, ...]:
    """为每处来源提及保留独立身份，供文档集合逐 Atom 签发。"""
    resolved: list[ResolvedSourceMention] = []
    for index, mention in enumerate(mentions):
        if index >= len(_SOURCE_SCOPE_KEYS):
            break
        resolution, allowed_documents = _resolve_mention(mention, documents)
        scope_key = _SOURCE_SCOPE_KEYS[index]
        scope_digest = canonical_sha256(
            {
                "schema": SOURCE_SCOPE_SCHEMA_REVISION,
                "scope_key": scope_key,
                "source_intent": intent.value,
                "resolution": resolution.value,
                "allowed_documents": [
                    item.model_dump(mode="json") for item in allowed_documents
                ],
                "required_content": required_content.value,
                "mention_sha256": canonical_sha256(mention),
                "registry_revision": registry_revision,
            }
        )
        resolved.append(
            ResolvedSourceMention(
                scope_key=scope_key,
                text=mention,
                resolution=resolution,
                allowed_documents=allowed_documents,
                scope_digest=scope_digest,
            )
        )
    return tuple(resolved)


def query_requires_source_resolution(query: str) -> bool:
    """判断原问是否包含必须在业务分析前解析的来源权限。"""
    _intent, mentions = _source_mentions(query)
    return bool(mentions)


def resolve_query_source_context(
    query: str,
    documents: tuple[CatalogDocument, ...],
    *,
    registry_revision: str,
) -> ResolvedSourceContext:
    """在 QueryAnalyzer 和 Planner 前冻结来源身份及业务问题视图。"""
    intent, mentions = _source_mentions(query)
    required_content = _required_content(query, intent)
    mention_scopes = _resolved_source_mentions(
        intent,
        mentions,
        documents,
        required_content=required_content,
        registry_revision=registry_revision,
    )
    resolution, allowed_documents = _resolve_mentions(
        intent, mentions, documents
    )
    scope_digest = canonical_sha256(
        {
            "schema": SOURCE_SCOPE_SCHEMA_REVISION,
            "source_intent": intent.value,
            "resolution": resolution.value,
            "allowed_documents": [
                item.model_dump(mode="json") for item in allowed_documents
            ],
            "required_content": required_content.value,
            "mention_sha256": [canonical_sha256(item) for item in mentions],
            "registry_revision": registry_revision,
        }
    )
    return ResolvedSourceContext(
        query_view=_build_query_view(
            query,
            intent=intent,
            mentions=mentions,
            scope_digest=scope_digest,
            mention_scope_digests=(
                tuple(item.scope_digest for item in mention_scopes)
                if intent is SourceIntent.DOCUMENT_SET
                else tuple(scope_digest for _mention in mentions)
            ),
        ),
        source_intent=intent,
        mentions=mentions,
        mention_scopes=mention_scopes,
        resolution=resolution,
        allowed_documents=allowed_documents,
        required_content=required_content,
        registry_revision=registry_revision,
        scope_digest=scope_digest,
    )


def _atom_source_scope_keys(atom: QueryAtom) -> tuple[str, ...]:
    """读取 Planner 从业务视图保留下来的不透明来源键。"""
    text = unicodedata.normalize(
        "NFKC",
        " ".join(
            part
            for part in (
                atom.target,
                atom.relation,
                atom.original_fragment,
                atom.source_qualifier,
            )
            if part
        ),
    ).casefold()
    return tuple(key for key in _SOURCE_SCOPE_KEYS if key.casefold() in text)


def resolve_query_plan_source_scopes(
    query_plan: QueryPlan,
    documents: tuple[CatalogDocument, ...],
    *,
    registry_revision: str,
    root_context: ResolvedSourceContext | None = None,
) -> QueryPlan:
    """为 QueryPlan 的每个 Atom 签发精确文档身份许可。

    Planner 的来源字段可以补充原问，但原问中已明确的来源不会因为
    Planner 漏字段而退回开放查询。所有匹配均基于活动目录的精确标签或
    受信别名，不使用向量排名或标题子串。

    Args:
        query_plan: 已完成原子拆分的当前查询计划。
        documents: 当前请求可见的完整活动文档目录。
        registry_revision: 当前目录与解析策略的稳定修订身份。
        root_context: 可选的业务分析前来源解析结果；显式来源存在时必须复用。

    Returns:
        携带逐 Atom ``SourceScopeDecision`` 且重新签名的 QueryPlan。

    """
    if root_context is not None and (
        root_context.query_view.original_query != query_plan.original_query
    ):
        raise ValueError("前置来源上下文与 QueryPlan 原问不一致。")
    root_intent, root_mentions = (
        (root_context.source_intent, root_context.mentions)
        if root_context is not None
        else _source_mentions(query_plan.original_query)
    )
    content = (
        root_context.required_content
        if root_context is not None
        else _required_content(query_plan.original_query, root_intent)
    )
    atom_scope_specs: list[
        tuple[
            QueryAtom,
            SourceIntent,
            SourceResolution,
            tuple[SourceDocumentIdentity, ...],
            str | None,
            SourceContentRequirement,
        ]
    ] = []
    for atom in query_plan.atoms:
        intent = root_intent
        mentions = root_mentions
        if not mentions and atom.source_qualifier:
            intent = SourceIntent.DOCUMENT_AUTHORITY
            mentions = (atom.source_qualifier,)
        if not mentions:
            atom_scope_specs.append(
                (
                    atom,
                    SourceIntent.OPEN,
                    SourceResolution.OPEN,
                    (),
                    None,
                    SourceContentRequirement.BODY,
                )
            )
            continue
        if intent is SourceIntent.DOCUMENT_SET:
            resolution, _allowed = (
                (root_context.resolution, root_context.allowed_documents)
                if root_context is not None and root_mentions
                else _resolve_mentions(intent, mentions, documents)
            )
            mention_scopes = (
                root_context.mention_scopes
                if root_context is not None and root_mentions
                else _resolved_source_mentions(
                    intent,
                    mentions,
                    documents,
                    required_content=content,
                    registry_revision=registry_revision,
                )
            )
            if resolution is not SourceResolution.RESOLVED:
                atom_scope_specs.append(
                    (
                        atom,
                        intent,
                        resolution,
                        (),
                        "\n".join(mentions),
                        content,
                    )
                )
                continue
            keys = set(_atom_source_scope_keys(atom))
            selected_scopes = tuple(
                item
                for item in mention_scopes
                if not keys or item.scope_key in keys
            )
            atom_scope_specs.extend(
                (
                    atom,
                    intent,
                    mention_scope.resolution,
                    mention_scope.allowed_documents,
                    mention_scope.text,
                    content,
                )
                for mention_scope in selected_scopes
            )
            continue
        if root_context is not None and root_mentions:
            resolution = root_context.resolution
            allowed = root_context.allowed_documents
        else:
            resolution, allowed = _resolve_mentions(
                intent,
                mentions,
                documents,
            )
        atom_scope_specs.append(
            (
                atom,
                intent,
                resolution,
                allowed,
                "\n".join(mentions),
                content,
            )
        )

    if len(atom_scope_specs) > _MAX_QUERY_ATOMS:
        atom_scope_specs = [
            (
                atom,
                root_intent,
                SourceResolution.AMBIGUOUS,
                (),
                "\n".join(root_mentions),
                content,
            )
            for atom in query_plan.atoms
        ]
    scoped_atoms: list[QueryAtom] = []
    for index, (
        atom,
        intent,
        resolution,
        allowed,
        mention,
        required_content,
    ) in enumerate(atom_scope_specs, 1):
        atom_id = f"A{index}"
        scope = _decision(
            atom_id=atom_id,
            source_intent=intent,
            resolution=resolution,
            allowed_documents=allowed,
            required_content=required_content,
            mention=mention,
            registry_revision=registry_revision,
        )
        scoped_atoms.append(
            atom.model_copy(update={"atom_id": atom_id, "source_scope": scope})
        )
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
    "ResolvedSourceContext",
    "build_resolved_query_view",
    "evidence_allowed_for_atom",
    "filter_identities_for_scope",
    "normalize_source_label",
    "query_plan_requires_source_resolution",
    "query_requires_source_resolution",
    "resolve_query_plan_source_scopes",
    "resolve_query_source_context",
    "source_identity_allowed",
]
