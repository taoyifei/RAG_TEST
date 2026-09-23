"""把结构化来源投影为来源可直接表达的最终事实文本。"""

from __future__ import annotations

import re

from rag_app.application.answering.evidence_binding import (
    BoundClaim,
    RenderOrigin,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.generation_packet import (
    EvidenceReadUnit,
    stable_read_unit_digest,
)
from rag_app.core.models.query_plan import (
    SourceContentRequirement,
    SourceScopeDecision,
)
from rag_app.core.models.retrieval import (
    AtomFactBinding,
    EvidenceItem,
    PhysicalTableFact,
)
from rag_app.core.query_text import (
    explicit_table_row_level_conflicts,
    literal_relation_modifiers_supported,
)
from rag_app.core.source_compatibility import reconstruct_complete_node_text

SOURCE_PROJECTION_REVISION = "wb08r-source-projection-v4"
_MAX_CLAIM_TEXT_CHARS = 6000
_MIN_EXTRACTIVE_SENTENCE_CHARS = 8
_MAX_EXTRACTIVE_SENTENCE_CHARS = 320
_MAX_SOURCE_TITLE_CHARS = 120
_QUANTITY = re.compile(
    r"\d+(?:\.\d+)?(?:个)?(?:工作日|自然日|分钟|小时|日|天|周|月|年)"
)


class SourceProjectionError(ValueError):
    """服务端无法从已绑定来源形成安全事实投影。"""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


def _ordered_texts(
    support_ids: tuple[str, ...], registry: dict[str, EvidenceItem]
) -> tuple[str, ...]:
    """按物理事实登记顺序读取逐字来源，并稳定去重。"""
    try:
        return tuple(
            dict.fromkeys(
                registry[support_id].citation_text.strip()
                for support_id in support_ids
                if registry[support_id].citation_text.strip()
            )
        )
    except KeyError as error:
        raise SourceProjectionError("PROJECTION_SOURCE_MISSING") from error


def render_physical_table_fact(
    fact: PhysicalTableFact, registry: dict[str, EvidenceItem]
) -> str:
    """只按真实行名、规范表头和值形成关系中性的表格陈述。

    Args:
        fact: 已由解析器坐标闭合的单个表格事实。
        registry: 当前请求已授权来源的 Support ID 映射。

    Returns:
        不增加时序、义务或因果关系的最终事实文本。

    Raises:
        SourceProjectionError: 来源身份或必要行列表达不闭合。

    """
    if any(
        registry[support_id].document_id != fact.document_id
        or registry[support_id].document_version_id != fact.document_version_id
        for support_id in fact.all_support_ids
        if support_id in registry
    ):
        raise SourceProjectionError("TABLE_FACT_SOURCE_IDENTITY_MISMATCH")
    row_labels = _ordered_texts(fact.row_label_support_ids, registry)
    values: tuple[str, ...]
    if fact.value_origin_row_index is not None:
        try:
            inherited_sources = tuple(
                registry[support_id] for support_id in fact.value_support_ids
            )
        except KeyError as error:
            raise SourceProjectionError("PROJECTION_SOURCE_MISSING") from error
        inherited_text = reconstruct_complete_node_text(inherited_sources)
        if inherited_text is None:
            raise SourceProjectionError("INHERITED_TABLE_VALUE_INCOMPLETE")
        values = (inherited_text.strip(),)
    else:
        values = _ordered_texts(fact.value_support_ids, registry)
    headers = tuple(
        "".join(_ordered_texts(header.support_ids, registry)).strip()
        for header in fact.headers
    )
    headers = tuple(dict.fromkeys(item for item in headers if item))
    if not row_labels or not headers or not values:
        raise SourceProjectionError("TABLE_FACT_PROJECTION_INCOMPLETE")
    return (
        f"资料中，‘{' / '.join(row_labels)}’行在"
        f"‘{' / '.join(headers)}’列的值为：{' / '.join(values)}。"
    )


def _finish_projection(  # noqa: PLR0913
    claim: BoundClaim,
    *,
    text: str,
    render_origin: RenderOrigin,
    selected_assertion_ids: tuple[str, ...],
    relation_complete: bool,
    relation_gap_reason: str | None = None,
    selected_units: tuple[EvidenceReadUnit, ...] | None = None,
) -> BoundClaim:
    """统一执行长度门并写入来源投影审计字段。"""
    if not text.strip() or len(text) > _MAX_CLAIM_TEXT_CHARS:
        raise SourceProjectionError("SOURCE_PROJECTION_TEXT_INVALID")
    retained_units = selected_units
    retained_support_ids = (
        {
            support_id
            for unit in retained_units
            for support_id in unit.support_ids
        }
        if retained_units is not None
        else None
    )
    return claim.with_projection(
        text=text,
        render_origin=render_origin,
        selected_assertion_ids=selected_assertion_ids,
        relation_complete=relation_complete,
        relation_gap_reason=relation_gap_reason,
        selected_unit_ids=(
            tuple(unit.unit_id for unit in retained_units)
            if retained_units is not None
            else None
        ),
        provenance=(
            tuple(
                support
                for support in claim.provenance
                if support.support_id in retained_support_ids
            )
            if retained_support_ids is not None
            else None
        ),
        physical_fact_ids=(
            tuple(
                unit.fact_id
                for unit in retained_units
                if unit.fact_id is not None
            )
            if retained_units is not None
            else None
        ),
    )


def _extractive_time_fact(
    selected: tuple[EvidenceReadUnit, ...],
    registry: dict[str, EvidenceItem],
) -> tuple[str, EvidenceItem] | None:
    """仅从一个完整正文来源恢复逐字时限事实，不拼接相邻来源。"""
    if len(selected) != 1:
        return None
    unit = selected[0]
    if (
        unit.kind not in {"paragraph", "list_item"}
        or not unit.source_complete
        or len(unit.support_ids) != 1
    ):
        return None
    item = registry[unit.support_ids[0]]
    sentence = item.citation_text.strip()
    if (
        item.table_context
        or "\n" in sentence
        or not _MIN_EXTRACTIVE_SENTENCE_CHARS
        <= len(sentence)
        <= _MAX_EXTRACTIVE_SENTENCE_CHARS
        or _QUANTITY.search(sentence) is None
        or sum(sentence.count(mark) for mark in "。！？!?") > 1
    ):
        return None
    return sentence, item


def _source_sentence_text(sentence: str, item: EvidenceItem) -> str:
    """只标识真实来源文档，不从标题推断正文未写出的条件。"""
    title = (item.display_name or "").strip()
    if title and len(title) <= _MAX_SOURCE_TITLE_CHARS:
        return f"《{title}》记载：{sentence}"
    return f"资料记载：{sentence}"


def project_bound_claim(  # noqa: PLR0913
    claim: BoundClaim,
    *,
    read_units: tuple[EvidenceReadUnit, ...],
    evidence: tuple[EvidenceItem, ...],
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
    atom_fact_bindings: tuple[AtomFactBinding, ...] = (),
    source_scope: SourceScopeDecision | None = None,
    semantic_relation_supported: bool = False,
    question_fragment: str = "",
    original_query: str = "",
) -> BoundClaim:
    """对表格、表格片段和目录项使用服务端最终表述。

    对单个完整正文时限事实保留来源原句。表格先核对物理闭合，
    再用当前子问题的语义复核与逐字限定决定能否保留自然表达。
    """
    units_by_id = {unit.unit_id: unit for unit in read_units}
    try:
        selected = tuple(units_by_id[item] for item in claim.selected_unit_ids)
    except KeyError as error:
        raise SourceProjectionError("PROJECTION_READ_UNIT_MISSING") from error
    registry = {item.support_id: item for item in evidence}
    facts = {fact.fact_id: fact for fact in physical_table_facts}
    table_units = tuple(unit for unit in selected if unit.fact_id is not None)
    if table_units:
        selected_fact_ids = tuple(
            unit.fact_id for unit in table_units if unit.fact_id is not None
        )
        try:
            selected_facts = tuple(
                facts[fact_id] for fact_id in selected_fact_ids
            )
        except KeyError as error:
            raise SourceProjectionError(
                "PROJECTION_TABLE_FACT_MISSING"
            ) from error
        if any(
            explicit_table_row_level_conflicts(
                text,
                _ordered_texts(fact.row_label_support_ids, registry),
            )
            for text in (original_query, claim.text)
            if text
            for fact in selected_facts
        ):
            raise SourceProjectionError("QUESTION_TABLE_ROW_LEVEL_CONFLICT")
        texts = tuple(
            render_physical_table_fact(fact, registry)
            for fact in selected_facts
        )
        source_text = " ".join(
            registry[support_id].citation_text
            for fact in selected_facts
            for support_id in fact.all_support_ids
        )
        binding_states = {
            binding.fact_id: binding.relation_status
            for binding in atom_fact_bindings
            if binding.atom_id == claim.atom_id
        }
        certified = all(
            binding_states.get(fact.fact_id) == "SUPPORTED"
            for fact in selected_facts
        )
        natural_supported = semantic_relation_supported and all(
            (
                literal_relation_modifiers_supported(fragment, source_text)
                and all(
                    quantity in source_text
                    for quantity in _QUANTITY.findall(fragment)
                )
            )
            for fragment in (question_fragment, claim.text)
        )
        complete = certified or natural_supported
        return _finish_projection(
            claim,
            text=claim.text if natural_supported else "\n".join(texts),
            render_origin="physical_table_fact",
            selected_assertion_ids=tuple(
                fact.fact_id for fact in selected_facts
            ),
            relation_complete=complete,
            relation_gap_reason=(
                None if complete else "TABLE_RELATION_UNDETERMINED"
            ),
            selected_units=table_units,
        )
    fragments = tuple(
        unit
        for unit in selected
        if dict(unit.source_context).get("structure_scope")
        == "literal_table_fragment"
    )
    if fragments:
        return _finish_projection(
            claim,
            text="资料相关片段记载："
            + "；".join(f"‘{unit.text.strip()}’" for unit in fragments)
            + "。",
            render_origin="literal_table_fragment",
            selected_assertion_ids=tuple(
                canonical_sha256(
                    {
                        "revision": SOURCE_PROJECTION_REVISION,
                        "read_unit": stable_read_unit_digest(unit),
                    }
                )
                for unit in fragments
            ),
            relation_complete=False,
            relation_gap_reason="TABLE_FRAGMENT_RELATION_INCOMPLETE",
            selected_units=fragments,
        )
    catalog_units = tuple(
        unit for unit in selected if unit.kind == "catalog_entry"
    )
    if catalog_units:
        existence_only = (
            source_scope is not None
            and source_scope.required_content
            is SourceContentRequirement.EXISTENCE
        )
        return _finish_projection(
            claim,
            text="资料目录显示存在以下条目："
            + "；".join(f"‘{unit.text.strip()}’" for unit in catalog_units)
            + "。",
            render_origin="catalog_entry",
            selected_assertion_ids=tuple(
                canonical_sha256(
                    {
                        "revision": SOURCE_PROJECTION_REVISION,
                        "read_unit": stable_read_unit_digest(unit),
                    }
                )
                for unit in catalog_units
            ),
            relation_complete=existence_only,
            relation_gap_reason=(
                None if existence_only else "CATALOG_BODY_UNAVAILABLE"
            ),
            selected_units=catalog_units,
        )
    time_fact = _extractive_time_fact(selected, registry)
    if time_fact is not None:
        sentence, item = time_fact
        return _finish_projection(
            claim,
            text=(
                _source_sentence_text(sentence, item)
                if semantic_relation_supported
                else sentence
            ),
            render_origin="source_sentence",
            selected_assertion_ids=(
                canonical_sha256(
                    {
                        "revision": SOURCE_PROJECTION_REVISION,
                        "source": item.support_id,
                        "sentence": sentence,
                    }
                ),
            ),
            relation_complete=semantic_relation_supported,
        )
    return _finish_projection(
        claim,
        text=claim.text,
        render_origin="model_text",
        selected_assertion_ids=tuple(
            canonical_sha256(
                {
                    "revision": SOURCE_PROJECTION_REVISION,
                    "read_unit": stable_read_unit_digest(unit),
                }
            )
            for unit in selected
        ),
        relation_complete=True,
    )


__all__ = [
    "SOURCE_PROJECTION_REVISION",
    "SourceProjectionError",
    "project_bound_claim",
    "render_physical_table_fact",
]
