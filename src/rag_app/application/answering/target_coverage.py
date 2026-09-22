"""以当前 Atom 的目标成员集合核对完整性，不把物理组当作答案。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    EvidenceGroup,
    EvidenceItem,
    SourceSpan,
)
from rag_app.core.models.evidence_group import EvidenceGroupKind, GroupSourceMap
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query import QuerySemantics
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from rag_app.core.query_text import (
    duty_heading_path_owns_target,
    literal_relation_modifiers_supported,
    normalize_document_label,
    normalize_duty_heading_label,
    query_without_source_qualifier,
    select_unique_label_owner,
    table_axis_label_in_query,
)
from rag_app.core.source_compatibility import (
    source_group_contains,
    source_group_covered,
    table_cell_coordinate,
)


@dataclass(frozen=True, slots=True)
class TargetMemberCoverage:
    """保留独立的来源闭合、必要事实及最终已接受事实覆盖。"""

    required_member_keys: tuple[str, ...] = ()
    covered_member_keys: tuple[str, ...] = ()
    missing_member_keys: tuple[str, ...] = ()
    member_support_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    selected_group_ids: tuple[str, ...] = ()
    source_complete: bool = False
    reason_codes: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """只有来源闭合且每个任务成员均被事实覆盖时才完整。"""
        return (
            bool(self.required_member_keys)
            and self.source_complete
            and not self.missing_member_keys
        )


def target_member_coverage(  # noqa: PLR0913
    atom: QueryAtom,
    evidence: tuple[EvidenceItem, ...],
    claims: tuple[AnswerClaim, ...],
    *,
    trusted_groups: tuple[EvidenceGroup, ...],
    semantics: QuerySemantics,
    fact_covered: Callable[[EvidenceItem, tuple[AnswerClaim, ...]], bool],
) -> TargetMemberCoverage:
    """按同一规范语义选择成员，再复用实际 Claim 内容核验函数。

    Args:
        atom: 当前 Atom；此函数不重新解释或改写用户问题。
        evidence: 实际可用于本 Atom 的已授权来源。
        claims: 已经过来源和问题关系验证的最终事实。
        trusted_groups: 从 canonical Chunk 独立构造的结构组。
        semantics: 最终关系验证器使用的同一份当前 Atom 语义。
        fact_covered: 现有事实内容覆盖检查；不能仅用引用存在代替内容。

    Returns:
        稳定成员身份、缺项及来源闭合诊断；不作任何模型调用。

    """
    required: dict[str, dict[str, EvidenceItem]] = {}
    fragment_covered: dict[str, dict[str, bool]] = {}
    member_support_ids: dict[str, list[str]] = {}
    selected: list[str] = []
    reasons: list[str] = []
    source_complete = True
    for group in trusted_groups:
        available = tuple(
            item for item in evidence if source_group_contains(item, group)
        )
        if not _scope_matches(group, semantics):
            continue
        template = next(
            (
                item
                for item in evidence
                if item.document_id == group.document_id
                and item.document_version_id == group.document_version_id
            ),
            None,
        )
        if template is None:
            continue
        canonical = _canonical_members(group, template)
        selection = _target_members(atom, semantics, group, canonical)
        if selection is None:
            continue
        facts, context = selection
        selected.append(group.group_id)
        projection = _project_group(group, (*facts, *context))
        if not source_group_covered(available, projection):
            source_complete = False
            reasons.append("TARGET_STRUCTURE_INCOMPLETE")
        for member in facts:
            key = _member_key(member)
            fragment_key = stable_support_key(member)
            required.setdefault(key, {})[fragment_key] = member
            member_group = _project_group(group, (member,))
            supporting = tuple(
                item
                for item in available
                if source_group_contains(item, member_group)
            )
            support_ids = member_support_ids.setdefault(key, [])
            support_ids.extend(item.support_id for item in supporting)
            is_covered = source_group_covered(supporting, member_group) and all(
                fact_covered(item, claims) for item in supporting
            )
            fragment_states = fragment_covered.setdefault(key, {})
            fragment_states[fragment_key] = (
                fragment_states.get(fragment_key, False) or is_covered
            )
    if not required:
        reasons.append("TARGET_MEMBER_SET_UNPROVED")
        source_complete = False
    covered = {
        key
        for key, members in required.items()
        if members
        and all(
            fragment_covered.get(key, {}).get(fragment_key, False)
            for fragment_key in members
        )
    }
    missing = tuple(key for key in required if key not in covered)
    if missing:
        reasons.append("TARGET_MEMBERS_NOT_ANSWERED")
    return TargetMemberCoverage(
        required_member_keys=tuple(required),
        covered_member_keys=tuple(key for key in required if key in covered),
        missing_member_keys=missing,
        member_support_ids=tuple(
            (key, tuple(dict.fromkeys(member_support_ids.get(key, ()))))
            for key in required
        ),
        selected_group_ids=tuple(dict.fromkeys(selected)),
        source_complete=source_complete,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _member_key(item: EvidenceItem) -> str:
    """按规范来源节点合并同一业务成员的多个 chunk 片段。"""
    node_ids = tuple(
        dict.fromkeys(
            span.node_id
            for span in item.source_spans
            if span.node_id is not None
        )
    )
    if not node_ids:
        return stable_support_key(item)
    return canonical_sha256(
        {
            "document_id": item.document_id,
            "document_version_id": item.document_version_id,
            "node_ids": node_ids,
        }
    )


def _scope_matches(group: EvidenceGroup, semantics: QuerySemantics) -> bool:
    """来源限定与阶段限定只消费受信字段，不从模型事实中借用。"""
    source = normalize_document_label(semantics.source_qualifier or "")
    if source and source not in normalize_document_label(group.display_name):
        return False
    context = normalize_document_label(semantics.context_qualifier or "")
    return not context or any(
        context in normalize_document_label(heading)
        for heading in group.heading_path
    )


def _canonical_members(
    group: EvidenceGroup, template: EvidenceItem
) -> tuple[EvidenceItem, ...]:
    """从独立原始映射取成员，缺失的已发送片段也仍留在目标集合。"""
    result: list[EvidenceItem] = []
    for mapping in group.member_source_maps:
        for span in mapping.source_spans:
            if not span.is_citable:
                continue
            text = mapping.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ]
            if not text:
                continue
            result.append(
                template.model_copy(
                    update={
                        "chunk_id": mapping.chunk_id,
                        "citation_text": text,
                        "source_spans": (
                            span.model_copy(
                                update={
                                    "chunk_start_char": 0,
                                    "chunk_end_char": len(text),
                                }
                            ),
                        ),
                        "heading_path": group.heading_path,
                    }
                )
            )
    return tuple(result)


def _target_members(
    atom: QueryAtom,
    semantics: QuerySemantics,
    group: EvidenceGroup,
    members: tuple[EvidenceItem, ...],
) -> tuple[tuple[EvidenceItem, ...], tuple[EvidenceItem, ...]] | None:
    target = normalize_duty_heading_label(semantics.target or atom.target)
    if not target:
        return None
    if group.kind is EvidenceGroupKind.TABLE_ROW_GROUP:
        return _table_members(atom, semantics, group, members, target)
    intros = tuple(
        member
        for member in members
        if member.citation_text.strip().endswith(("：", ":"))
    )
    owns_target = duty_heading_path_owns_target(
        target, group.heading_path
    ) or any(
        normalize_duty_heading_label(item.citation_text.rstrip("：:")) == target
        for item in intros
    )
    if not owns_target:
        return None
    facts = tuple(member for member in members if member not in intros)
    if semantics.ordinal is not None:
        index = semantics.ordinal - 1
        if index >= len(facts):
            return None
        facts = (facts[index],)
    return (facts, intros) if facts else None


def _table_members(
    atom: QueryAtom,
    semantics: QuerySemantics,
    group: EvidenceGroup,
    members: tuple[EvidenceItem, ...],
    target: str,
) -> tuple[tuple[EvidenceItem, ...], tuple[EvidenceItem, ...]] | None:
    headers = dict(group.metadata).get("canonical_header_node_ids")
    if not isinstance(headers, (list, tuple)):
        return None
    located = tuple(
        (member, cell)
        for member in members
        if (cell := table_cell_coordinate(member)) is not None
    )
    question_body = query_without_source_qualifier(
        atom.original_fragment or "", atom.source_qualifier
    )
    row_labels = tuple(
        (member, cell)
        for member, cell in located
        if cell[2] == 0 and member.source_spans[0].node_id not in headers
    )
    unique_row_key = select_unique_label_owner(
        target,
        (
            (stable_support_key(member), member.citation_text)
            for member, _cell in row_labels
        ),
    )
    labels = tuple(
        (member, cell)
        for member, cell in row_labels
        if (
            stable_support_key(member) == unique_row_key
            or normalize_duty_heading_label(member.citation_text) == target
            or table_axis_label_in_query(question_body, member.citation_text)
        )
    )
    relation = normalize_document_label(semantics.relation or atom.relation)
    selected_headers = tuple(
        (member, cell)
        for member, cell in located
        if member.source_spans[0].node_id in headers
        and cell[2] > 0
        and _relation_header_matches(atom, relation, member, question_body)
    )
    if not literal_relation_modifiers_supported(
        question_body,
        " ".join(member.citation_text for member, _cell in selected_headers),
    ):
        return None
    facts: list[EvidenceItem] = []
    context: list[EvidenceItem] = []
    for label, row in labels:
        for header, column in selected_headers:
            if row[0] != column[0]:
                continue
            values = tuple(
                member
                for member, coordinate in located
                if coordinate == (row[0], row[1], column[2])
                and member.source_spans[0].node_id not in headers
            )
            if values:
                facts.extend(values)
                context.extend((label, header))
    return (tuple(facts), tuple(context)) if facts else None


def _relation_header_matches(
    atom: QueryAtom,
    relation: str,
    member: EvidenceItem,
    question_body: str,
) -> bool:
    header = normalize_document_label(member.citation_text)
    if relation and (relation == header or relation in header):
        return True
    if table_axis_label_in_query(question_body, member.citation_text):
        return True
    # 此处仅识别请求的字段类型；主体与阶段另由完整规范标签选定。
    return atom.answer_shape is AtomAnswerShape.DUTIES and "职责" in header


def _project_group(
    group: EvidenceGroup, members: tuple[EvidenceItem, ...]
) -> EvidenceGroup:
    """保留完整性原判定，仅投影所问成员的真实 SourceSpan。"""
    maps: list[GroupSourceMap] = []
    for chunk_id in dict.fromkeys(item.chunk_id for item in members):
        originals = tuple(item for item in members if item.chunk_id == chunk_id)
        text = ""
        spans: list[SourceSpan] = []
        seen: set[str] = set()
        for item in originals:
            key = stable_support_key(item)
            if key in seen:
                continue
            seen.add(key)
            offset = len(text)
            spans.extend(
                span.model_copy(
                    update={
                        "chunk_start_char": span.chunk_start_char + offset,
                        "chunk_end_char": span.chunk_end_char + offset,
                    }
                )
                for span in item.source_spans
            )
            text += item.citation_text
        maps.append(
            GroupSourceMap(
                chunk_id=chunk_id, citation_text=text, source_spans=tuple(spans)
            )
        )
    return group.model_copy(
        update={
            "member_source_maps": tuple(maps),
            "member_chunk_ids": tuple(item.chunk_id for item in maps),
            "member_ranks": tuple(range(1, len(maps) + 1)),
        }
    )
