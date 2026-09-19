"""完整回答服务只接受当前 attempt 的实际发送来源与 Atom 许可。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import GroundedAnsweringService
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    ConfidenceDecision,
    ConfidenceStatus,
)
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_support_key,
)
from rag_app.core.models.query_plan import AtomStatus
from rag_app.core.models.retrieval import ClaimSupport
from rag_app.core.ports import GenerationRequest
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
    _pack,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)


def _packet(
    request: GenerationRequest, sent: tuple[str, ...]
) -> PreparedGenerationPacket:
    """以固定 HTTP 替身的最终发送集合建立 SAFE 包。"""
    registry = tuple(
        (item.support_id, stable_support_key(item))
        for item in request.evidence
        if item.support_id in sent
    )
    return PreparedGenerationPacket(
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        packet_id=f"packet-{request.attempt_id}",
        schema_revision="unit-synthetic-v8",
        evidence_level="TRANSPORT_SENT",
        alias_to_support_key=registry,
        per_atom_support_ids=tuple(
            (atom, tuple(sid for sid in ids if sid in sent))
            for atom, ids in request.per_atom_candidate_support_ids
        ),
        original_support_keys=tuple(
            stable_support_key(item) for item in request.evidence
        ),
        messages_sha256="sha256:" + "a" * 64,
        estimated_input_tokens=100,
        max_input_tokens=2000,
        reserved_output_tokens=300,
        safety_margin_tokens=100,
    )


def test_service_rejects_an_alias_omitted_from_transport_packet() -> None:
    evidence = _evidence("甲部门负责交付。", "乙部门负责巡检。")
    source = next(item for item in evidence if "甲部门" in item.citation_text)
    other = next(item for item in evidence if item is not source)
    plan = _plan("甲部门")

    def generate(request: GenerationRequest) -> AnswerDraft:
        return _draft(
            (_claim("C1", source.citation_text, "A1", source.support_id),), plan
        ).model_copy(
            update={"prepared_packet": _packet(request, (other.support_id,))}
        )

    generator = Mock()
    generator.generate.side_effect = generate
    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.accepted_claim_count == 0
    assert outcome.repair_calls == 0
    assert outcome.raw_failures == (("A1", "CLAIM_SUPPORT_OUTSIDE_ATOM"),)
    assert outcome.prepared_packets[0].sent_support_ids == (other.support_id,)
    assert generator.generate.call_count == 1


@pytest.mark.parametrize(
    "field", ["request_id", "attempt_id", "alias_to_support_key"]
)
def test_service_rejects_packet_from_another_identity(field: str) -> None:
    evidence = _evidence("甲部门负责交付。")
    plan = _plan("甲部门")

    def generate(request: GenerationRequest) -> AnswerDraft:
        packet = _packet(request, ("S1",))
        value = (
            (("S1", "sha256:" + "b" * 64),)
            if field == "alias_to_support_key"
            else "another-attempt"
        )
        return _draft(
            (_claim("C1", evidence[0].citation_text, "A1", "S1"),), plan
        ).model_copy(
            update={"prepared_packet": packet.model_copy(update={field: value})}
        )

    generator = Mock()
    generator.generate.side_effect = generate
    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.answer is None
    assert outcome.accepted_claim_count == 0
    assert outcome.reason_code == "GENERATION_PACKET_IDENTITY_MISMATCH"
    assert outcome.repair_calls == 0
    assert generator.generate.call_count == 1


def test_repair_keeps_first_attempt_claim_and_has_new_registry_identity() -> (
    None
):
    evidence = _evidence("甲部门负责交付。", "乙部门负责巡检。")
    plan = _plan("甲部门", "乙部门")
    by_role = {
        "A1": next(item for item in evidence if "甲部门" in item.citation_text),
        "A2": next(item for item in evidence if "乙部门" in item.citation_text),
    }
    requests: list[GenerationRequest] = []

    def generate(request: GenerationRequest) -> AnswerDraft:
        requests.append(request)
        atom_id = "A2" if request.repair_atom_ids else "A1"
        source = by_role[atom_id]
        return _draft(
            (_claim("C1", source.citation_text, atom_id, source.support_id),),
            plan,
        ).model_copy(
            update={"prepared_packet": _packet(request, (source.support_id,))}
        )

    generator = Mock()
    generator.generate.side_effect = generate
    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()), (AtomStatus.MISSING, ())),
    )

    assert len(requests) == 2
    assert requests[1].repair_atom_ids == ("A2",)
    assert requests[1].accepted_claim_ids == ("C1",)
    assert requests[1].request_id == requests[0].request_id
    assert requests[1].attempt_id != requests[0].attempt_id
    assert outcome.accepted_claim_count == 2
    assert len(outcome.prepared_packets) == 2
    assert outcome.atom_coverage == (("A1", "SUPPORTED"), ("A2", "SUPPORTED"))


def test_legacy_stream_fallback_retains_both_transport_packets() -> None:
    evidence = _evidence("甲部门负责交付。")

    def generate(request: GenerationRequest) -> AnswerDraft:
        previous = _packet(request, ("S1",))
        final = previous.model_copy(
            update={
                "attempt_id": "sync-fallback",
                "packet_id": "packet-sync-fallback",
            }
        )
        claim = AnswerClaim(
            text=evidence[0].citation_text,
            supports=(
                ClaimSupport(support_id="S1", quote=evidence[0].citation_text),
            ),
        )
        return AnswerDraft(
            text=claim.text,
            cited_evidence_ids=("S1",),
            claims=(claim,),
            generation_mode="llm",
            prepared_packet=final,
            previous_prepared_packets=(previous,),
        )

    generator = Mock()
    generator.generate.side_effect = generate
    outcome = GroundedAnsweringService(generator).answer(
        "甲部门的职责是什么？",
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
    )

    assert outcome.answer == "甲部门负责交付。 [S1]"
    assert len(outcome.prepared_packets) == 2
    assert (
        outcome.prepared_packets[0].request_id
        == outcome.prepared_packets[1].request_id
    )
    assert (
        outcome.prepared_packets[0].attempt_id
        != outcome.prepared_packets[1].attempt_id
    )


@pytest.mark.parametrize("missing_side", [False, True])
def test_conflicting_sources_use_current_aliases_or_fail_closed(
    missing_side: bool,
) -> None:
    sources = {
        item.citation_text: item
        for item in _evidence(
            "甲部门保存记录 10 天。",
            "甲部门保存记录 20 天。",
            "乙部门负责巡检。",
        )
    }
    left = sources["甲部门保存记录 10 天。"].model_copy(
        update={"evidence_id": "S1"}
    )
    right = sources["甲部门保存记录 20 天。"].model_copy(
        update={"evidence_id": "S2"}
    )
    unrelated = sources["乙部门负责巡检。"].model_copy(
        update={"evidence_id": "S1"}
    )
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.CONTRADICTORY, ("S1", "S2")),))
    matrix = matrix.model_copy(
        update={
            "atoms": (
                matrix.atoms[0].model_copy(
                    update={
                        "supporting_support_keys": (
                            stable_support_key(left),
                            stable_support_key(right),
                        )
                    }
                ),
            )
        }
    )
    final = (
        unrelated,
        left.model_copy(update={"evidence_id": "S2"}),
        *(
            (right.model_copy(update={"evidence_id": "S3"}),)
            if not missing_side
            else ()
        ),
    )
    generator = Mock()
    outcome = GroundedAnsweringService(generator).answer(
        plan.standalone_query,
        (left, right),
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=matrix,
        generation_evidence_pack=_pack(plan, final),
    )

    generator.generate.assert_not_called()
    if missing_side:
        assert outcome.answer is None
        assert outcome.published_support_ids == ()
    else:
        assert outcome.answer is not None
        assert "甲部门保存记录 10 天。 [S2]" in outcome.answer
        assert "甲部门保存记录 20 天。 [S3]" in outcome.answer
        assert "乙部门负责巡检。" not in outcome.answer
        assert outcome.published_support_ids == ("S2", "S3")
