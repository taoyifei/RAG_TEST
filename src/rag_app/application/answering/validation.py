"""P07 Support ID、quote 和 extractive claim 发布校验。"""

from __future__ import annotations

from rag_app.core.errors import ValidationFailed
from rag_app.core.models import AnswerDraft, EvidenceItem
from rag_app.core.models.chunk import SourceSpanKind


def validate_extractive_draft(
    draft: AnswerDraft, evidence: tuple[EvidenceItem, ...]
) -> None:
    """拒绝未知 Support、跨 separator quote 和 Evidence 外文本。

    Args:
        draft: Generator 返回的未发布草稿。
        evidence: 应用分配 Support ID 的证据集。

    Returns:
        无返回值。

    Raises:
        ValidationFailed: 草稿包含未知引用或 Evidence 外内容。

    """
    by_id = {item.evidence_id: item for item in evidence}
    if not draft.cited_evidence_ids or any(
        support_id not in by_id for support_id in draft.cited_evidence_ids
    ):
        raise ValidationFailed(
            "回答包含未知或空 Support ID。", stage="answer.validate"
        )
    cited_quotes = {
        by_id[support_id].citation_text.strip()
        for support_id in draft.cited_evidence_ids
    }
    if any(
        not item.source_spans
        or any(
            not span.is_citable or span.span_type is SourceSpanKind.SEPARATOR
            for span in item.source_spans
        )
        for item in evidence
        if item.evidence_id in draft.cited_evidence_ids
    ):
        raise ValidationFailed(
            "回答引用跨越不可发布 SourceSpan。", stage="answer.validate"
        )
    claims = tuple(
        line.strip() for line in draft.text.splitlines() if line.strip()
    )
    if not claims or any(claim not in cited_quotes for claim in claims):
        raise ValidationFailed(
            "Extractive 回答引入 Evidence 外文本。", stage="answer.validate"
        )


__all__ = ["validate_extractive_draft"]
