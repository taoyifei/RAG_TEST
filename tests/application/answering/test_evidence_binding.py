"""短阅读引用恢复完整 provenance 的确定性边界。"""

from __future__ import annotations

import pytest

from rag_app.application.answering.evidence_binding import (
    EvidenceBindingError,
    bind_wire_claim,
    revalidate_bound_claim,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AtomFactBinding,
    EvidenceItem,
    EvidenceReadUnit,
    GroundedWireClaim,
    PhysicalTableFact,
    PhysicalTableHeader,
)
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_support_key,
)
from rag_app.core.models.query_plan import (
    SourceContentRequirement,
    SourceDocumentIdentity,
    SourceIntent,
    SourceResolution,
    SourceScopeDecision,
)
from tests.application.answering.test_natural_grounded_answer import _evidence


def _fixture() -> tuple[
    tuple[EvidenceItem, ...],
    PhysicalTableFact,
    EvidenceReadUnit,
    AtomFactBinding,
]:
    base = _evidence(*(f"物理跨度 {index}" for index in range(1, 9)))
    evidence = (
        *base,
        base[-1].model_copy(
            update={"evidence_id": "S9", "citation_text": "物理跨度 9"}
        ),
        base[-1].model_copy(
            update={"evidence_id": "S10", "citation_text": "物理跨度 10"}
        ),
    )
    fact_id = "sha256:" + "1" * 64
    fact = PhysicalTableFact(
        fact_id=fact_id,
        table_key="sha256:" + "2" * 64,
        document_id="doc_" + "3" * 32,
        document_version_id="dver_" + "4" * 32,
        table_node_id="node_" + "5" * 32,
        row_index=2,
        row_label_column_index=0,
        value_column_index=2,
        row_label_support_ids=("S1",),
        value_support_ids=("S2", "S3", "S4", "S5", "S6", "S7", "S8"),
        headers=(
            PhysicalTableHeader(
                row_index=0,
                column_indexes=(2,),
                support_ids=("S9", "S10"),
            ),
        ),
    )
    unit = EvidenceReadUnit(
        unit_id="E1",
        kind="table_fact",
        text="行、列和值组成的完整物理事实",
        support_ids=fact.all_support_ids,
        fact_id=fact_id,
        source_complete=True,
    )
    binding = AtomFactBinding(
        atom_id="A1",
        fact_id=fact_id,
        requested_target="目标行",
        requested_relation="对应值",
    )
    return evidence, fact, unit, binding


def _scope(
    item: EvidenceItem, *, digest_seed: str = "a"
) -> SourceScopeDecision:
    assert item.document_id is not None
    assert item.document_version_id is not None
    return SourceScopeDecision(
        atom_id="A1",
        source_intent=SourceIntent.DOCUMENT_AUTHORITY,
        resolution=SourceResolution.RESOLVED,
        allowed_documents=(
            SourceDocumentIdentity(
                document_id=item.document_id,
                document_version_id=item.document_version_id,
            ),
        ),
        required_content=SourceContentRequirement.BODY,
        mention_sha256=canonical_sha256("指定文档"),
        registry_revision="test-registry-v1",
        scope_digest=f"sha256:{digest_seed * 64}",
    )


def test_one_model_ref_restores_more_than_eight_physical_spans() -> None:
    evidence, fact, unit, binding = _fixture()
    claim = bind_wire_claim(
        GroundedWireClaim(atom_id="A1", text="完整表格事实", refs=("E1",)),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    assert len(claim.selected_unit_ids) == 1
    assert len(claim.provenance) == 10
    assert tuple(item.support_id for item in claim.provenance) == (
        fact.all_support_ids
    )


def test_table_fact_cannot_be_borrowed_by_another_atom() -> None:
    evidence, fact, unit, binding = _fixture()

    with pytest.raises(
        EvidenceBindingError, match="TABLE_FACT_BINDING_INVALID"
    ):
        bind_wire_claim(
            GroundedWireClaim(atom_id="A2", text="借用表格事实", refs=("E1",)),
            claim_id="C1",
            read_units=(unit,),
            evidence=evidence,
            allowed_unit_ids=frozenset({"E1"}),
            physical_table_facts=(fact,),
            atom_fact_bindings=(binding,),
        )


def test_revalidation_detects_read_unit_identity_change() -> None:
    evidence, fact, unit, binding = _fixture()
    claim = bind_wire_claim(
        GroundedWireClaim(atom_id="A1", text="完整表格事实", refs=("E1",)),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )
    packet = PreparedGenerationPacket(
        request_id="request",
        attempt_id="attempt",
        packet_id=canonical_sha256("packet"),
        schema_revision="test",
        evidence_level="TRANSPORT_SENT",
        alias_to_support_key=tuple(
            (item.support_id, stable_support_key(item)) for item in evidence
        ),
        read_unit_bindings=(("E1", (stable_support_key(evidence[0]),)),),
        per_atom_read_unit_ids=(("A1", ("E1",)),),
        original_support_keys=tuple(
            stable_support_key(item) for item in evidence
        ),
        messages_sha256=canonical_sha256("messages"),
        transport_body_sha256=canonical_sha256("transport"),
        estimated_input_tokens=1,
        max_input_tokens=10,
        reserved_output_tokens=1,
        safety_margin_tokens=1,
    )

    with pytest.raises(
        EvidenceBindingError, match="READ_UNIT_IDENTITY_CHANGED"
    ):
        revalidate_bound_claim(
            claim,
            packet=packet,
            read_units=(unit,),
            evidence=evidence,
            allowed_unit_ids=frozenset({"E1"}),
            physical_table_facts=(fact,),
            atom_fact_bindings=(binding,),
        )


def test_binding_rejects_real_but_wrong_document() -> None:
    evidence = _evidence("错误文档中的真实句子。")
    item = evidence[0]
    unit = EvidenceReadUnit(
        unit_id="E1",
        kind="paragraph",
        text=item.citation_text,
        support_ids=(item.support_id,),
        source_complete=True,
    )
    wrong_scope = _scope(
        item.model_copy(
            update={
                "document_id": f"doc_{'e' * 32}",
                "document_version_id": f"dver_{'f' * 32}",
            }
        )
    )

    with pytest.raises(EvidenceBindingError, match="DOCUMENT_SCOPE_MISMATCH"):
        bind_wire_claim(
            GroundedWireClaim(
                atom_id="A1", text="看似有来源的回答。", refs=("E1",)
            ),
            claim_id="C1",
            read_units=(unit,),
            evidence=evidence,
            allowed_unit_ids=frozenset({"E1"}),
            source_scope=wrong_scope,
        )


def test_revalidation_rejects_changed_source_scope_digest() -> None:
    evidence = _evidence("指定文档中的真实句子。")
    item = evidence[0]
    unit = EvidenceReadUnit(
        unit_id="E1",
        kind="paragraph",
        text=item.citation_text,
        support_ids=(item.support_id,),
        source_complete=True,
    )
    scope = _scope(item)
    claim = bind_wire_claim(
        GroundedWireClaim(atom_id="A1", text="回答。", refs=("E1",)),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        source_scope=scope,
    ).with_semantic_status("supported")
    support_key = stable_support_key(item)
    packet = PreparedGenerationPacket(
        request_id="request",
        attempt_id="attempt",
        packet_id=canonical_sha256("packet"),
        schema_revision="test",
        evidence_level="TRANSPORT_SENT",
        alias_to_support_key=((item.support_id, support_key),),
        read_unit_bindings=(("E1", (support_key,)),),
        per_atom_read_unit_ids=(("A1", ("E1",)),),
        per_atom_source_scope_digests=(("A1", f"sha256:{'b' * 64}"),),
        original_support_keys=(support_key,),
        messages_sha256=canonical_sha256("messages"),
        transport_body_sha256=canonical_sha256("transport"),
        estimated_input_tokens=1,
        max_input_tokens=10,
        reserved_output_tokens=1,
        safety_margin_tokens=1,
    )

    with pytest.raises(
        EvidenceBindingError, match="SOURCE_SCOPE_IDENTITY_CHANGED"
    ):
        revalidate_bound_claim(
            claim,
            packet=packet,
            read_units=(unit,),
            evidence=evidence,
            allowed_unit_ids=frozenset({"E1"}),
            source_scope=scope,
        )
