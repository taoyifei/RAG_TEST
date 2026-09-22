"""来源范围的低层身份 predicate，供检索与回答边界共同复用。"""

from __future__ import annotations

from collections.abc import Iterable

from rag_app.core.models.query_plan import (
    SourceContentRequirement,
    SourceResolution,
    SourceScopeDecision,
)
from rag_app.core.models.retrieval import EvidenceItem


def source_identity_allowed(
    decision: SourceScopeDecision | None,
    *,
    document_id: str | None,
    document_version_id: str | None,
) -> bool:
    """核对一个真实来源身份是否属于当前 Atom 的许可集合。"""
    if decision is None or decision.resolution is SourceResolution.OPEN:
        return True
    if decision.resolution is not SourceResolution.RESOLVED:
        return False
    return any(
        item.document_id == document_id
        and item.document_version_id == document_version_id
        for item in decision.allowed_documents
    )


def evidence_allowed_for_atom(
    decision: SourceScopeDecision | None,
    evidence: EvidenceItem,
    purpose: SourceContentRequirement = SourceContentRequirement.BODY,
) -> bool:
    """统一核对证据所有权和目录/正文用途。"""
    if not source_identity_allowed(
        decision,
        document_id=evidence.document_id,
        document_version_id=evidence.document_version_id,
    ):
        return False
    if decision is None or decision.resolution is SourceResolution.OPEN:
        return True
    metadata = dict(evidence.metadata)
    catalog_entry = metadata.get(
        "evidence_group_type"
    ) == "CATALOG_ENTRY" or evidence.citation_text.startswith("模板目录项：")
    return not (
        catalog_entry and purpose is not SourceContentRequirement.EXISTENCE
    )


def filter_identities_for_scope(
    decision: SourceScopeDecision | None,
    identities: Iterable[tuple[str | None, str | None]],
) -> tuple[tuple[str | None, str | None], ...]:
    """按同一来源 predicate 过滤轻量检索身份，供候选阶段复用。"""
    return tuple(
        identity
        for identity in identities
        if source_identity_allowed(
            decision,
            document_id=identity[0],
            document_version_id=identity[1],
        )
    )


__all__ = [
    "evidence_allowed_for_atom",
    "filter_identities_for_scope",
    "source_identity_allowed",
]
