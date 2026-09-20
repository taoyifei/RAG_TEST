"""批量语义结果必须逐事实闭合，漏项保持 unknown。"""

from __future__ import annotations

import pytest

from rag_app.application.answering.evidence_binding import BoundClaim
from rag_app.application.answering.semantic_validation import (
    SemanticValidationCandidate,
    SemanticValidationPayload,
    SemanticValidationRequest,
    SemanticValidationResult,
    normalized_semantic_results,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import EvidenceReadUnit
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_read_unit_digest,
)
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom


def _request() -> SemanticValidationRequest:
    unit = EvidenceReadUnit(
        unit_id="E1",
        kind="paragraph",
        text="甲部门负责归档。",
        support_ids=("S1",),
        source_complete=True,
    )
    packet = PreparedGenerationPacket(
        request_id="request",
        attempt_id="generation",
        packet_id=canonical_sha256("packet"),
        schema_revision="test",
        evidence_level="TRANSPORT_SENT",
        alias_to_support_key=(("S1", "sha256:" + "1" * 64),),
        read_unit_bindings=(("E1", ("sha256:" + "1" * 64,)),),
        read_unit_sha256s=(("E1", stable_read_unit_digest(unit)),),
        per_atom_read_unit_ids=(("A1", ("E1",)),),
        original_support_keys=("sha256:" + "1" * 64,),
        messages_sha256=canonical_sha256("messages"),
        transport_body_sha256=canonical_sha256("transport"),
        estimated_input_tokens=1,
        max_input_tokens=10,
        reserved_output_tokens=1,
        safety_margin_tokens=1,
    )
    atom = QueryAtom(
        atom_id="A1",
        target="甲部门",
        relation="职责",
        answer_shape=AtomAnswerShape.FACT,
    )
    candidates = tuple(
        SemanticValidationCandidate(
            claim=BoundClaim(
                claim_id=f"C{index}",
                atom_id="A1",
                text=f"事实 {index}",
                selected_unit_ids=("E1",),
                provenance=(),
                physical_fact_ids=(),
            ),
            atom=atom,
        )
        for index in (1, 2)
    )
    return SemanticValidationRequest(
        original_query="甲部门做什么？",
        candidates=candidates,
        read_units=(unit,),
        sent_packet=packet,
        request_id="request",
        attempt_id="semantic",
        deadline_monotonic=10**12,
    )


def test_missing_model_result_is_explicit_unknown() -> None:
    results = normalized_semantic_results(
        SemanticValidationPayload(
            results=(
                SemanticValidationResult(claim_id="C1", status="supported"),
            )
        ),
        _request(),
    )

    assert tuple((item.claim_id, item.status) for item in results) == (
        ("C1", "supported"),
        ("C2", "unknown"),
    )


def test_unknown_or_duplicate_result_id_rejects_the_batch() -> None:
    request = _request()
    with pytest.raises(ValueError, match="UNKNOWN_CLAIM"):
        normalized_semantic_results(
            SemanticValidationPayload(
                results=(
                    SemanticValidationResult(claim_id="C3", status="supported"),
                )
            ),
            request,
        )
    with pytest.raises(ValueError, match="DUPLICATE_RESULT"):
        normalized_semantic_results(
            SemanticValidationPayload(
                results=(
                    SemanticValidationResult(claim_id="C1", status="supported"),
                    SemanticValidationResult(claim_id="C1", status="unknown"),
                )
            ),
            request,
        )


def test_review_rejects_read_unit_text_changed_after_generation() -> None:
    request = _request()
    changed = request.read_units[0].model_copy(
        update={"text": "乙部门负责归档。"}
    )

    with pytest.raises(ValueError, match="READ_UNIT_CHANGED"):
        SemanticValidationRequest(
            original_query=request.original_query,
            candidates=request.candidates,
            read_units=(changed,),
            sent_packet=request.sent_packet,
            request_id=request.request_id,
            attempt_id="semantic-changed",
            deadline_monotonic=request.deadline_monotonic,
        )
