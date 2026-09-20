"""把结构化来源投影为来源可直接表达的最终事实文本。"""

from __future__ import annotations

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

SOURCE_PROJECTION_REVISION = "wb08r-source-projection-v2"
_MAX_CLAIM_TEXT_CHARS = 6000


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


def _table_fact_text(
    fact: PhysicalTableFact, registry: dict[str, EvidenceItem]
) -> str:
    """只按真实行名、规范表头和值形成关系中性的表格陈述。"""
    if any(
        registry[support_id].document_id != fact.document_id
        or registry[support_id].document_version_id != fact.document_version_id
        for support_id in fact.all_support_ids
        if support_id in registry
    ):
        raise SourceProjectionError("TABLE_FACT_SOURCE_IDENTITY_MISMATCH")
    row_labels = _ordered_texts(fact.row_label_support_ids, registry)
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


def project_bound_claim(  # noqa: PLR0913
    claim: BoundClaim,
    *,
    read_units: tuple[EvidenceReadUnit, ...],
    evidence: tuple[EvidenceItem, ...],
    physical_table_facts: tuple[PhysicalTableFact, ...] = (),
    atom_fact_bindings: tuple[AtomFactBinding, ...] = (),
    source_scope: SourceScopeDecision | None = None,
) -> BoundClaim:
    """对表格、表格片段和目录项使用服务端最终表述。

    普通段落仍保留模型文本；结构来源则不允许模型自行增加时序、义务
    或因果关系。关系是否完整独立记录，不能由语义复核的 ``supported``
    状态替代。
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
        texts = tuple(
            _table_fact_text(fact, registry) for fact in selected_facts
        )
        binding_states = {
            binding.fact_id: binding.relation_status
            for binding in atom_fact_bindings
            if binding.atom_id == claim.atom_id
        }
        complete = all(
            binding_states.get(fact.fact_id) == "SUPPORTED"
            for fact in selected_facts
        )
        return _finish_projection(
            claim,
            text="\n".join(texts),
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
]
