"""旧查询与生成结果到 Core 外壳的单向转换。"""

from __future__ import annotations

from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import AnswerResult, EvidenceItem
from rag_app.generation.answer import AnswerResult as LegacyAnswerResult
from rag_app.generation.evidence import EvidenceItem as LegacyEvidenceItem
from rag_app.query_service import QueryOutcome

_TRACE_ID_LENGTH = 38


def legacy_evidence_to_core(value: LegacyEvidenceItem) -> EvidenceItem:
    """把旧 EvidenceItem 显式投影到 Core 外壳。

    Args:
        value: 旧回答链证据。

    Returns:
        不携带 embedding 或基础设施对象的 Core 证据。

    """
    source_label = (
        value.locators[0].display() if value.locators else value.source_id
    )
    return EvidenceItem(
        evidence_id=value.evidence_id,
        chunk_id=value.chunk_id,
        citation_text=value.text,
        source_label=source_label,
        metadata=(
            ("low_confidence_ocr", value.low_confidence_ocr),
            ("rerank_rank", value.rerank_rank),
            ("rerank_score", value.rerank_score),
        ),
    )


def legacy_answer_to_core(
    value: LegacyAnswerResult,
    *,
    trace_id: str,
    evidence: tuple[EvidenceItem, ...] = (),
) -> AnswerResult:
    """把旧 AnswerResult 投影到最小 Core 回答外壳。

    Args:
        value: 旧回答结果。
        trace_id: 新 Core trace ID。
        evidence: 已单独转换的证据。

    Returns:
        保留回答或稳定拒答原因的 Core 外壳。

    """
    reason_code = (
        value.refusal_code.value
        if value.refusal_code is not None
        else value.status.value
    )
    answer = value.answer or value.user_message or reason_code
    return AnswerResult(
        answer=answer,
        evidence=evidence,
        trace_id=trace_id,
        reason_code=reason_code,
    )


def legacy_query_outcome_to_core(value: QueryOutcome) -> AnswerResult:
    """转换旧 QueryOutcome，并规范旧 trace ID。

    Args:
        value: 完整旧查询结果。

    Returns:
        Core 回答外壳。

    """
    trace_id = (
        value.trace_id
        if value.trace_id.startswith("trace_")
        and len(value.trace_id) == _TRACE_ID_LENGTH
        else deterministic_id("trace", value.trace_id)
    )
    return legacy_answer_to_core(value.answer, trace_id=trace_id)
