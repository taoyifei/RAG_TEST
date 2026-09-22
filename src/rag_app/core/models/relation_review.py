"""仅对已发送证据执行窄关系复核的内部合同。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from rag_app.core.models.common import FrozenModel
from rag_app.core.models.evidence_group import EvidenceGroup, EvidenceGroupKind
from rag_app.core.models.generation_packet import (
    PreparedGenerationPacket,
    stable_support_key,
)
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.query import QueryAnalysis
from rag_app.core.models.query_plan import QueryAtom
from rag_app.core.models.retrieval import EvidenceItem, NaturalClaim
from rag_app.core.source_compatibility import (
    source_group_contains,
    table_cell_coordinate,
)

RELATION_REVIEW_REVISION = "wb08r-relation-review-v4"
ReviewSourceId = Annotated[str, Field(pattern=r"^E[1-9][0-9]*$")]
ReviewAnchorId = Annotated[
    str, Field(pattern=r"^E[1-9][0-9]*Q[1-9][0-9]*$")
]


class RelationReviewCandidate(FrozenModel):
    """保持原 Claim、当前 Atom 和同一份受信分析，禁止模型改写事实。"""

    claim_id: str = Field(min_length=1, max_length=80)
    claim: NaturalClaim
    atom: QueryAtom
    analysis: QueryAnalysis
    context_support_ids: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _validate_atom(self) -> Self:
        if self.claim.atom_id != self.atom.atom_id:
            raise ValueError("RELATION_REVIEW_ATOM_MISMATCH")
        return self


def review_context_support_ids(
    candidate: RelationReviewCandidate,
    evidence: tuple[EvidenceItem, ...],
    packet: PreparedGenerationPacket,
    trusted_groups: tuple[EvidenceGroup, ...],
) -> tuple[str, ...]:
    """只闭合已发送表格值的规范行名与同列表头，不借其他行或列。"""
    registry = dict(packet.alias_to_support_key)
    allowed = set(
        dict(packet.per_atom_support_ids).get(candidate.atom.atom_id, ())
    )
    sent = tuple(
        item
        for item in evidence
        if item.support_id in allowed
        and item.publishable
        and item.source_spans
        and all(span.is_citable for span in item.source_spans)
        and registry.get(item.support_id) == stable_support_key(item)
    )
    by_id = {item.support_id: item for item in sent}
    selected = {support.support_id for support in candidate.claim.supports}
    contexts: set[str] = set()
    for support_id in selected:
        value = by_id.get(support_id)
        coordinate = table_cell_coordinate(value) if value is not None else None
        if value is None or coordinate is None or coordinate[2] <= 0:
            continue
        value_key = stable_support_key(value)
        reading_keys = set().union(
            *(
                set(keys)
                for owner, keys in packet.retained_source_units
                if owner in {"ROOT", candidate.atom.atom_id}
                and value_key in keys
            )
        )
        for group in trusted_groups:
            headers = dict(group.metadata).get("canonical_header_node_ids")
            if (
                group.kind is not EvidenceGroupKind.TABLE_ROW_GROUP
                or group.group_id not in packet.complete_group_ids
                or not isinstance(headers, (list, tuple))
                or not headers
                or not source_group_contains(value, group)
            ):
                continue
            labels: list[str] = []
            columns: list[str] = []
            for item in sent:
                if (
                    item.support_id in selected
                    or stable_support_key(item) not in reading_keys
                    or not source_group_contains(item, group)
                ):
                    continue
                cell = table_cell_coordinate(item)
                if cell is None or cell[0] != coordinate[0]:
                    continue
                nodes = {span.node_id for span in item.source_spans}
                is_header = bool(nodes) and nodes <= set(headers)
                if (
                    not is_header
                    and cell[1] == coordinate[1]
                    and cell[2] == 0
                ):
                    labels.append(item.support_id)
                if (
                    is_header
                    and cell[1] < coordinate[1]
                    and cell[2] == coordinate[2]
                ):
                    columns.append(item.support_id)
            # 缺规范表头或目标行名时保持保守，不用首行猜测、不补半组。
            if labels and columns:
                contexts.update((*labels, *columns))
    return tuple(
        item.support_id for item in sent if item.support_id in contexts
    )


class RelationReviewRequest(FrozenModel):
    """仅原发送包内合法来源可进入复核，不建立新请求预算。"""

    original_query: str = Field(min_length=1, repr=False)
    candidates: tuple[RelationReviewCandidate, ...] = Field(
        min_length=1, max_length=24
    )
    evidence: tuple[EvidenceItem, ...] = Field(min_length=1)
    trusted_source_groups: tuple[EvidenceGroup, ...] = ()
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
            if candidate.context_support_ids != review_context_support_ids(
                candidate, self.evidence, packet, self.trusted_source_groups
            ):
                raise ValueError("RELATION_REVIEW_CONTEXT_NOT_PROVED")
        return self


def review_source_quotes(
    request: RelationReviewRequest,
) -> dict[str, tuple[str, ...]]:
    """返回复核 HTTP 实际允许发送的逐来源引文。

    事实来源只发送原 Claim 已选的逐字引文；已认证结构语境才发送完整
    ``citation_text``。响应锚点必须受同一集合约束，不能借稳定来源身份读取
    首次请求没有发送的同节点正文。

    Args:
        request: 已通过首次发送身份校验的复核请求。

    Returns:
        以真实 Support ID 为键的去重逐字引文。

    """
    context_ids = {
        support_id
        for candidate in request.candidates
        for support_id in candidate.context_support_ids
    }
    return {
        item.support_id: (
            (item.citation_text,)
            if item.support_id in context_ids
            else tuple(
                dict.fromkeys(
                    support.quote
                    for candidate in request.candidates
                    for support in candidate.claim.supports
                    if support.support_id == item.support_id
                )
            )
        )
        for item in request.evidence
    }


class RelationReviewScope(FrozenModel):
    """语义标签与原文分离；模型只回传服务端签发的短锚点。"""

    relation_label: str = Field(default="", max_length=300)
    subject_anchor_ids: tuple[ReviewAnchorId, ...] = Field(
        default=(), max_length=8
    )
    relation_anchor_ids: tuple[ReviewAnchorId, ...] = Field(
        default=(), max_length=8
    )
    stage_anchor_ids: tuple[ReviewAnchorId, ...] = Field(
        default=(), max_length=8
    )
    condition_anchor_ids: tuple[ReviewAnchorId, ...] = Field(
        default=(), max_length=8
    )

    @model_validator(mode="after")
    def _reject_duplicate_anchors(self) -> Self:
        for values in (
            self.subject_anchor_ids,
            self.relation_anchor_ids,
            self.stage_anchor_ids,
            self.condition_anchor_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("RELATION_REVIEW_DUPLICATE_SCOPE_SOURCE")
        return self


class RelationReviewResult(FrozenModel):
    """受约束质量信号，不是发布授权或形式证明。"""

    claim_id: str = Field(min_length=1, max_length=80)
    status: Literal["supported", "irrelevant", "contradicted", "undetermined"]
    fact_source_ids: tuple[ReviewSourceId, ...] = Field(max_length=8)
    source_scope: RelationReviewScope

    @model_validator(mode="after")
    def _require_support(self) -> Self:
        if len(self.fact_source_ids) != len(set(self.fact_source_ids)):
            raise ValueError("RELATION_REVIEW_DUPLICATE_FACT_SOURCE")
        if self.status == "supported" and (
            not self.fact_source_ids
            or not self.source_scope.relation_label
            or not self.source_scope.relation_anchor_ids
        ):
            raise ValueError("RELATION_REVIEW_SUPPORTED_WITHOUT_SOURCE_ANCHOR")
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
    source_aliases = {
        item.support_id: f"E{index}"
        for index, item in enumerate(request.evidence, start=1)
    }
    evidence_by_id = {item.support_id: item for item in request.evidence}
    anchor_sources = {
        f"{source_aliases[support_id]}Q{index}": source_aliases[support_id]
        for support_id, quotes in review_source_quotes(request).items()
        for index, _quote in enumerate(quotes, start=1)
    }
    for result in payload.results:
        candidate = candidates[result.claim_id]
        original = {
            source_aliases[support.support_id]
            for support in candidate.claim.supports
        }
        returned = set(result.fact_source_ids)
        if not returned <= original:
            raise ValueError("RELATION_REVIEW_FACT_SOURCE_CHANGED")
        if result.status == "supported" and returned != original:
            raise ValueError("RELATION_REVIEW_SUPPORT_SET_INCOMPLETE")
        context = {
            source_aliases[support_id]
            for support_id in candidate.context_support_ids
        }
        if result.status == "supported" and any(
            (coordinate := table_cell_coordinate(
                evidence_by_id[support.support_id]
            ))
            is not None
            and coordinate[2] > 0
            for support in candidate.claim.supports
        ) and not context:
            raise ValueError("RELATION_REVIEW_TABLE_CONTEXT_REQUIRED")
        anchors = (
            *result.source_scope.subject_anchor_ids,
            *result.source_scope.relation_anchor_ids,
            *result.source_scope.stage_anchor_ids,
            *result.source_scope.condition_anchor_ids,
        )
        if any(
            anchor_sources.get(anchor_id) not in original | context
            for anchor_id in anchors
        ):
            raise ValueError("RELATION_REVIEW_SCOPE_SOURCE_CHANGED")
