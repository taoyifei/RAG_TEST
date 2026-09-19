"""仅对已发送证据执行窄关系复核的内部合同。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_support_key,
)
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.query import QueryAnalysis
from rag_app.core.models.query_plan import QueryAtom
from rag_app.core.models.retrieval import EvidenceItem, NaturalClaim

RELATION_REVIEW_REVISION = "wb08r-relation-review-v1"


class RelationReviewCandidate(FrozenModel):
    """保持原 Claim、当前 Atom 和同一份受信分析，禁止模型改写事实。"""

    claim_id: str = Field(min_length=1, max_length=80)
    claim: NaturalClaim
    atom: QueryAtom
    analysis: QueryAnalysis

    @model_validator(mode="after")
    def _validate_atom(self) -> Self:
        if self.claim.atom_id != self.atom.atom_id:
            raise ValueError("RELATION_REVIEW_ATOM_MISMATCH")
        return self


class RelationReviewRequest(FrozenModel):
    """仅原发送包内合法来源可进入复核，不建立新请求预算。"""

    original_query: str = Field(min_length=1, repr=False)
    candidates: tuple[RelationReviewCandidate, ...] = Field(
        min_length=1, max_length=24
    )
    evidence: tuple[EvidenceItem, ...] = Field(min_length=1)
    sent_packet: PreparedGenerationPacket
    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    deadline_monotonic: float = Field(gt=0, allow_inf_nan=False)
    generation_model: str | None = Field(
        default=None, min_length=1, max_length=200
    )

    @model_validator(mode="after")
    def _validate_sent_sources(self) -> Self:
        packet = self.sent_packet
        if (
            packet.evidence_level != "TRANSPORT_SENT"
            or packet.request_id != self.request_id
        ):
            raise ValueError(
                "RELATION_REVIEW_REQUIRES_SAME_REQUEST_SENT_PACKET"
            )
        if packet.attempt_id == self.attempt_id:
            raise ValueError("RELATION_REVIEW_REQUIRES_NEW_ATTEMPT")
        registry = dict(packet.alias_to_support_key)
        evidence = {item.support_id: item for item in self.evidence}
        if len(evidence) != len(self.evidence):
            raise ValueError("RELATION_REVIEW_DUPLICATE_SUPPORT")
        if any(
            registry.get(alias) != stable_support_key(item)
            for alias, item in evidence.items()
        ):
            raise ValueError("RELATION_REVIEW_UNSENT_SOURCE")
        ids = [candidate.claim_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("RELATION_REVIEW_DUPLICATE_CLAIM")
        allowed = dict(packet.per_atom_support_ids)
        for candidate in self.candidates:
            for support in candidate.claim.supports:
                item = evidence.get(support.support_id)
                if item is None or support.support_id not in allowed.get(
                    candidate.atom.atom_id, ()
                ):
                    raise ValueError("RELATION_REVIEW_SUPPORT_OUTSIDE_ATOM")
                if support.quote not in item.citation_text:
                    raise ValueError("RELATION_REVIEW_QUOTE_NOT_VERBATIM")
        return self


class RelationReviewSupport(FrozenModel):
    """复核器必须回指实际发送的稳定身份与逐字引文。"""

    support_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    quote: str = Field(min_length=1, max_length=6000, repr=False)


class RelationReviewScope(FrozenModel):
    """模型声称覆盖的关系范围，仍须应用层重核硬约束。"""

    subject: str = Field(max_length=300)
    relation: str = Field(max_length=300)
    stage: str = Field(max_length=300)
    conditions: tuple[str, ...] = Field(max_length=16)


class RelationReviewResult(FrozenModel):
    """受约束质量信号，不是发布授权或形式证明。"""

    claim_id: str = Field(min_length=1, max_length=80)
    status: Literal["supported", "irrelevant", "contradicted", "undetermined"]
    supports: tuple[RelationReviewSupport, ...] = Field(max_length=8)
    covered_scope: RelationReviewScope

    @model_validator(mode="after")
    def _require_support(self) -> Self:
        if self.status == "supported" and not self.supports:
            raise ValueError("RELATION_REVIEW_SUPPORTED_WITHOUT_QUOTES")
        return self


class RelationReviewPayload(FrozenModel):
    """唯一允许的模型输出字段；没有改写文本、推理或覆盖状态。"""

    results: tuple[RelationReviewResult, ...] = Field(
        min_length=1, max_length=24
    )


class RelationReviewResponse(FrozenModel):
    """返回质量信号与实际 HTTP 账本，单列补充用途。"""

    results: tuple[RelationReviewResult, ...]
    call: ProviderCall
    prepared_packet: PreparedGenerationPacket
    purpose: Literal["relation_review"] = "relation_review"


def validate_review_payload(
    payload: RelationReviewPayload, request: RelationReviewRequest
) -> None:
    """重核标识、实际引用和逐字来源，不接受复核器新增或替换引文。"""
    candidates = {item.claim_id: item for item in request.candidates}
    ids = [item.claim_id for item in payload.results]
    if len(ids) != len(set(ids)) or set(ids) != set(candidates):
        raise ValueError("RELATION_REVIEW_CLAIM_SET_CHANGED")
    by_id = {item.support_id: item for item in request.evidence}
    for result in payload.results:
        candidate = candidates[result.claim_id]
        original = {
            (stable_support_key(by_id[s.support_id]), s.quote)
            for s in candidate.claim.supports
        }
        returned = {(s.support_key, s.quote) for s in result.supports}
        if len(returned) != len(result.supports) or not returned <= original:
            raise ValueError("RELATION_REVIEW_QUOTE_CHANGED")
        if result.status == "supported" and returned != original:
            raise ValueError("RELATION_REVIEW_SUPPORT_SET_INCOMPLETE")
