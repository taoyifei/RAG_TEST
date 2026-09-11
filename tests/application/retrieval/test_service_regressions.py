"""查询终态与正式引用复核的公开回归。"""

from rag_app.application.retrieval.service import (
    QueryDataPlaneContext,
    _formal_span_is_current,
    _model_capability_status,
)
from rag_app.core.models import ConfidenceStatus, EvidenceItem
from tests.application.retrieval.helpers import make_ranked_chunk


def test_unknown_corpus_blocker_does_not_recurse() -> None:
    """新的语料绑定原因仍按授权状态稳定拒绝，不触发递归。"""
    context = QueryDataPlaneContext(
        model_configuration_state="CONFIGURED",
        model_authorization_state="STALE_MODEL",
        corpus_authorization_state="APPROVED",
        budget_state="BLOCKED",
        fallback_reason_codes=("CORPUS_MODEL_BINDING_CHANGED",),
        report_model_capability_blockers=True,
    )

    assert _model_capability_status(
        context, "CORPUS_MODEL_BINDING_CHANGED"
    ) == (
        ConfidenceStatus.POLICY_DENIED,
        "CORPUS_MODEL_BINDING_CHANGED",
    )


def test_formal_span_recheck_accepts_trimmed_source_coordinates() -> None:
    """表格单元格去尾空白后同步缩短来源范围，不误报索引损坏。"""
    ranked = make_ranked_chunk(1, "OPC（One ")
    chunk = ranked.hydrated.chunk
    original = chunk.source_spans[0]
    span = original.model_copy(
        update={
            "chunk_start_char": 0,
            "chunk_end_char": 7,
            "source_start_char": 0,
            "source_end_char": 7,
        }
    )
    item = EvidenceItem(
        evidence_id="S1",
        chunk_id=chunk.chunk_id,
        citation_text="OPC（One",
        source_label="公开回归.docx",
        source_spans=(span,),
    )

    assert _formal_span_is_current(chunk, original, span, item)
