"""显式启用的零网络合成生成器，不修改产品缺模型时的失败关闭策略。"""

from __future__ import annotations

from rag_app.application.answering.semantic_validation import (
    SemanticValidationRequest,
    SemanticValidationResponse,
    SemanticValidationResult,
)
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    AnswerDraft,
    EvidenceItem,
    GroundedWireClaim,
    ProviderCall,
)
from rag_app.core.models.generation_packet import (
    EvidenceReadUnit,
    PreparedGenerationPacket,
    safe_support_source,
    stable_read_unit_digest,
    stable_support_key,
)
from rag_app.core.models.retrieval import ClaimSupport
from rag_app.core.ports.generator import GenerationRequest


class GroundedFixtureGenerator:
    """仅复述测试已选支持，所有事实仍由真实 Grounded 验证器核验。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.GENERATOR,
        name="unit-synthetic-grounded",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
    )
    capabilities = ComponentCapabilities()
    supplement_timeout_seconds = 5.0

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """按请求证据构造合成草稿，不读取题号、期望值或 Gold。"""
        self.calls += 1
        supports = _verified_fixture_supports(request)
        if not supports:
            if request.query_plan is not None:
                packet, _claims = _wire_fixture(request, ())
                return AnswerDraft(
                    text="合成生成器没有已选支持。",
                    cited_evidence_ids=(),
                    wire_claims=(),
                    generation_mode="natural",
                    prepared_packet=packet,
                    reason_code="GENERATION_ABSTAINED",
                )
            return AnswerDraft(
                text="合成生成器没有已选支持。",
                cited_evidence_ids=(),
                generation_mode="natural" if request.query_plan else "llm",
                reason_code="GENERATION_ABSTAINED",
            )
        if request.query_plan is None:
            claims = tuple(_quote_claim((item,)) for item in supports)
            return AnswerDraft(
                text="\n".join(claim.text for claim in claims),
                cited_evidence_ids=tuple(item.support_id for item in supports),
                claims=claims,
                generation_mode="llm",
            )
        packet, claims_by_atom = _wire_fixture(request, supports)
        return AnswerDraft(
            text="\n".join(claim.text for claim in claims_by_atom)
            or "没有合成事实。",
            cited_evidence_ids=packet.sent_support_ids,
            wire_claims=claims_by_atom,
            generation_mode="natural",
            prepared_packet=packet,
            reason_code=None if claims_by_atom else "GENERATION_ABSTAINED",
        )

    def review_semantics(
        self,
        request: SemanticValidationRequest,
    ) -> SemanticValidationResponse:
        """给出合成质量信号；产品硬门仍会重绑并重验全部来源。"""
        packet = request.sent_packet.model_copy(
            update={
                "attempt_id": request.attempt_id,
                "packet_id": canonical_sha256(
                    (request.attempt_id, "unit-semantic-review")
                ),
                "schema_revision": "unit-semantic-review-v1",
                "messages_sha256": canonical_sha256("unit-semantic-review"),
                "transport_body_sha256": canonical_sha256(
                    "unit-semantic-response"
                ),
            }
        )
        return SemanticValidationResponse(
            results=tuple(
                SemanticValidationResult(
                    claim_id=item.claim.claim_id,
                    status="supported",
                )
                for item in request.candidates
            ),
            call=ProviderCall(
                provider_id="unit-synthetic-grounded",
                operation="generation.semantic_review",
                call_count=0,
                retry_count=0,
                elapsed_ms=0,
                reason_code="UNIT_SYNTHETIC_REVIEW",
                model="unit-synthetic-grounded",
            ),
            prepared_packet=packet,
        )


def _wire_fixture(
    request: GenerationRequest,
    supports: tuple[EvidenceItem, ...],
) -> tuple[PreparedGenerationPacket, tuple[GroundedWireClaim, ...]]:
    """按当前 Wire 合同构造一次已发送的合成响应。"""
    if request.query_plan is None:
        raise ValueError("WIRE_FIXTURE_REQUIRES_QUERY_PLAN")
    units = request.evidence_read_units
    allowance = dict(request.per_atom_candidate_support_ids)
    verified_support_ids = frozenset(item.support_id for item in supports)
    fact_ids_by_atom = {
        atom.atom_id: {
            binding.fact_id
            for binding in request.atom_fact_bindings
            if binding.atom_id == atom.atom_id
        }
        for atom in request.query_plan.atoms
    }
    per_atom_units = tuple(
        (
            atom.atom_id,
            tuple(
                unit.unit_id
                for unit in units
                if _unit_allowed(
                    unit,
                    allowed_support_ids=frozenset(
                        support_id
                        for support_id in allowance.get(atom.atom_id, ())
                        if support_id in verified_support_ids
                    ),
                    allowed_fact_ids=frozenset(fact_ids_by_atom[atom.atom_id]),
                    verified_support_ids=verified_support_ids,
                )
            ),
        )
        for atom in request.query_plan.atoms
    )
    sent_unit_ids = {
        unit_id for _atom_id, unit_ids in per_atom_units for unit_id in unit_ids
    }
    sent_units = tuple(unit for unit in units if unit.unit_id in sent_unit_ids)
    sent_support_ids = {
        support_id for unit in sent_units for support_id in unit.support_ids
    }
    sent_evidence = tuple(
        item for item in request.evidence if item.support_id in sent_support_ids
    )
    keys_by_id = {
        item.support_id: stable_support_key(item) for item in sent_evidence
    }
    units_by_id = {unit.unit_id: unit for unit in sent_units}
    packet = PreparedGenerationPacket(
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        packet_id=canonical_sha256(
            (request.request_id, request.attempt_id, "unit-wire-fixture")
        ),
        schema_revision="unit-synthetic-wire-v1",
        evidence_level="TRANSPORT_SENT",
        alias_to_support_key=tuple(keys_by_id.items()),
        read_unit_bindings=tuple(
            (
                unit.unit_id,
                tuple(keys_by_id[item] for item in unit.support_ids),
            )
            for unit in sent_units
        ),
        read_unit_sha256s=tuple(
            (unit.unit_id, stable_read_unit_digest(unit)) for unit in sent_units
        ),
        per_atom_read_unit_ids=per_atom_units,
        per_atom_source_scope_digests=tuple(
            (atom.atom_id, atom.source_scope.scope_digest)
            for atom in request.query_plan.atoms
            if atom.source_scope is not None
        ),
        support_sources=tuple(
            safe_support_source(item) for item in sent_evidence
        ),
        per_atom_support_ids=tuple(
            (
                atom_id,
                tuple(
                    dict.fromkeys(
                        support_id
                        for unit_id in unit_ids
                        for support_id in units_by_id[unit_id].support_ids
                    )
                ),
            )
            for atom_id, unit_ids in per_atom_units
        ),
        original_support_keys=tuple(
            stable_support_key(item) for item in request.evidence
        ),
        messages_sha256=canonical_sha256("unit-wire-fixture"),
        transport_body_sha256=canonical_sha256("unit-wire-response"),
        estimated_input_tokens=1,
        max_input_tokens=6144,
        reserved_output_tokens=512,
        safety_margin_tokens=1,
    )
    claims = tuple(
        GroundedWireClaim(
            atom_id=atom_id,
            text=units_by_id[unit_id].text,
            refs=(unit_id,),
        )
        for atom_id, unit_ids in per_atom_units
        for unit_id in unit_ids
    )
    return packet, claims


def _unit_allowed(
    unit: EvidenceReadUnit,
    *,
    allowed_support_ids: frozenset[str],
    allowed_fact_ids: frozenset[str],
    verified_support_ids: frozenset[str],
) -> bool:
    """复现产品层逐 Atom 的阅读单元授权，不授予额外来源。"""
    if unit.fact_id is not None:
        return unit.fact_id in allowed_fact_ids and set(
            unit.support_ids
        ) <= set(verified_support_ids)
    if dict(unit.source_context).get("structure_scope") == (
        "literal_table_fragment"
    ):
        return False
    return set(unit.support_ids) <= allowed_support_ids


def _quote_claim(items: tuple[EvidenceItem, ...]) -> AnswerClaim:
    return AnswerClaim(
        text="\n".join(item.citation_text for item in items),
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in items
        ),
    )


def _verified_fixture_supports(
    request: GenerationRequest,
) -> tuple[EvidenceItem, ...]:
    """从当前请求矩阵读取合成支持，避免把 Root 可读集合当已证明事实。"""
    if request.answer_support_set or request.atom_support_matrix is None:
        return request.answer_support_set
    keys = {
        key
        for atom in request.atom_support_matrix.atoms
        if atom.status.value == "SUPPORTED"
        for key in atom.supporting_support_keys
    }
    return tuple(
        item for item in request.evidence if stable_support_key(item) in keys
    )


class HistoricalExtractiveFixtureGenerator:
    """仅供历史摘录协议回放的显式端口，不代表自然生成质量。"""

    descriptor = GroundedFixtureGenerator.descriptor.model_copy(
        update={"name": "unit-synthetic-historical-extractive"}
    )
    capabilities = ComponentCapabilities()

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """保留旧评测协议的逐字行，调用方仍须执行真实摘录验证器。"""
        self.calls += 1
        supports = request.answer_support_set
        return AnswerDraft(
            text="\n".join(item.citation_text for item in supports),
            cited_evidence_ids=tuple(item.support_id for item in supports),
            generation_mode="extractive",
        )
