"""显式启用的零网络合成生成器，不修改产品缺模型时的失败关闭策略。"""

from __future__ import annotations

from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.models import AnswerClaim, AnswerDraft, EvidenceItem
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
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

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """按请求证据构造合成草稿，不读取题号、期望值或 Gold。"""
        self.calls += 1
        supports = _verified_fixture_supports(request)
        if not supports:
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
        allowance = dict(request.per_atom_candidate_support_ids)
        claims_by_atom = tuple(
            NaturalClaim(
                atom_id=atom.atom_id,
                text=item.citation_text,
                supports=(
                    ClaimSupport(
                        support_id=item.support_id, quote=item.citation_text
                    ),
                ),
            )
            for atom in request.query_plan.atoms
            for item in supports
            if item.support_id in allowance.get(atom.atom_id, ())
        )
        return AnswerDraft(
            text="\n".join(claim.text for claim in claims_by_atom)
            or "没有合成事实。",
            cited_evidence_ids=tuple(item.support_id for item in supports),
            natural_claims=claims_by_atom,
            generation_mode="natural",
            reason_code=None if claims_by_atom else "GENERATION_ABSTAINED",
        )


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
