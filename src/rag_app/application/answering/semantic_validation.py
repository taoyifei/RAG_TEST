"""自然事实的一次批量语义判定合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from rag_app.application.answering.evidence_binding import BoundClaim
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.generation_packet import (
    EvidenceReadUnit,
    PreparedGenerationPacket,
    stable_read_unit_digest,
)
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.query_plan import QueryAtom

SEMANTIC_VALIDATION_REVISION = "wb08r-semantic-validation-v3"
_MAX_CANDIDATES = 24


class SemanticValidationResult(FrozenModel):
    """模型只返回服务端签发的事实 ID 与三态语义结果。"""

    claim_id: str = Field(pattern=r"^C[1-9][0-9]*$")
    status: Literal["supported", "contradicted", "unknown"]


class SemanticValidationPayload(FrozenModel):
    """批量语义复核的唯一输出对象。"""

    results: tuple[SemanticValidationResult, ...] = Field(max_length=24)


@dataclass(frozen=True, slots=True)
class SemanticValidationCandidate:
    """服务端绑定事实、对应子问和已发送阅读单元。"""

    claim: BoundClaim
    atom: QueryAtom

    def __post_init__(self) -> None:
        """核对事实与子问属于同一个 Atom。"""
        if self.claim.atom_id != self.atom.atom_id:
            raise ValueError("SEMANTIC_VALIDATION_ATOM_MISMATCH")


@dataclass(frozen=True, slots=True)
class SemanticValidationRequest:
    """只能复核首次生成实际发送并绑定成功的事实。"""

    original_query: str
    candidates: tuple[SemanticValidationCandidate, ...]
    read_units: tuple[EvidenceReadUnit, ...]
    sent_packet: PreparedGenerationPacket
    request_id: str
    attempt_id: str
    deadline_monotonic: float
    generation_model: str | None = None

    def __post_init__(self) -> None:
        """核对本次复核只消费首次调用实际发送的阅读单元。"""
        if (
            not self.original_query
            or not self.candidates
            or not self.read_units
        ):
            raise ValueError("SEMANTIC_VALIDATION_EMPTY_INPUT")
        if not 0 < len(self.candidates) <= _MAX_CANDIDATES:
            raise ValueError("SEMANTIC_VALIDATION_CANDIDATE_COUNT")
        packet = self.sent_packet
        if (
            packet.evidence_level != "TRANSPORT_SENT"
            or packet.request_id != self.request_id
            or packet.attempt_id == self.attempt_id
        ):
            raise ValueError("SEMANTIC_VALIDATION_REQUIRES_SENT_PACKET")
        unit_ids = {unit.unit_id for unit in self.read_units}
        sent_ids = set(packet.sent_read_unit_ids)
        if unit_ids - sent_ids or any(
            not set(candidate.claim.selected_unit_ids) <= unit_ids
            for candidate in self.candidates
        ):
            raise ValueError("SEMANTIC_VALIDATION_UNSENT_READ_UNIT")
        aliases = dict(packet.alias_to_support_key)
        bindings = dict(packet.read_unit_bindings)
        digests = dict(packet.read_unit_sha256s)
        for unit in self.read_units:
            if any(
                support_id not in aliases for support_id in unit.support_ids
            ):
                raise ValueError("SEMANTIC_VALIDATION_UNKNOWN_SUPPORT")
            mapped_keys = tuple(
                aliases[support_id] for support_id in unit.support_ids
            )
            if mapped_keys != bindings[unit.unit_id]:
                raise ValueError("SEMANTIC_VALIDATION_READ_UNIT_CHANGED")
            if digests and digests.get(unit.unit_id) != stable_read_unit_digest(
                unit
            ):
                raise ValueError("SEMANTIC_VALIDATION_READ_UNIT_CHANGED")
        claim_ids = [candidate.claim.claim_id for candidate in self.candidates]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("SEMANTIC_VALIDATION_DUPLICATE_CLAIM")


@dataclass(frozen=True, slots=True)
class SemanticValidationResponse:
    """语义三态结果及其实际 Provider 与发送包账本。"""

    results: tuple[SemanticValidationResult, ...]
    call: ProviderCall
    prepared_packet: PreparedGenerationPacket


def normalized_semantic_results(
    payload: SemanticValidationPayload,
    request: SemanticValidationRequest,
) -> tuple[SemanticValidationResult, ...]:
    """拒绝未知或重复 ID，并把模型遗漏的候选明确保持 unknown。"""
    expected = {
        candidate.claim.claim_id: candidate for candidate in request.candidates
    }
    returned: dict[str, SemanticValidationResult] = {}
    for result in payload.results:
        if result.claim_id not in expected:
            raise ValueError("SEMANTIC_VALIDATION_UNKNOWN_CLAIM")
        if result.claim_id in returned:
            raise ValueError("SEMANTIC_VALIDATION_DUPLICATE_RESULT")
        returned[result.claim_id] = result
    return tuple(
        returned.get(
            claim_id,
            SemanticValidationResult(claim_id=claim_id, status="unknown"),
        )
        for claim_id in expected
    )
