"""查询终态与正式引用复核的公开回归。"""

import pytest

from rag_app.application.retrieval.service import (
    QueryDataPlaneContext,
    _formal_span_is_current,
    _model_capability_status,
)
from rag_app.core.models import ConfidenceStatus, EvidenceItem, SourceSpanKind
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


def test_invalid_generated_claims_are_not_projected_as_provider_outage() -> (
    None
):
    """模型返回但 claim 结构失败应继续按证据不足安全拒答。"""
    context = QueryDataPlaneContext(
        generation_provider_id="openai-compatible",
        generation_model="synthetic-model",
        model_configuration_state="CONFIGURED",
    )

    assert (
        _model_capability_status(context, "GENERATION_CLAIMS_INVALID") is None
    )


def test_semantic_rejection_is_not_projected_as_provider_outage() -> None:
    """复核成功但无可发布事实时应保持证据不足终态。"""
    context = QueryDataPlaneContext(
        generation_provider_id="openai-compatible",
        generation_model="synthetic-model",
        model_configuration_state="CONFIGURED",
    )

    assert (
        _model_capability_status(context, "SEMANTIC_REVIEW_NO_SUPPORTED_CLAIM")
        is None
    )
    assert _model_capability_status(
        context, "SEMANTIC_REVIEW_PROVIDER_ERROR"
    ) == (
        ConfidenceStatus.PROVIDER_UNAVAILABLE,
        "SEMANTIC_REVIEW_PROVIDER_ERROR",
    )


@pytest.mark.parametrize(
    "reason",
    (
        "GENERATION_JSON_DECODE_FAILED",
        "GENERATION_WIRE_SCHEMA_ROOT_FIELDS",
        "GENERATION_ITEMS_REJECTED",
        "GENERATION_OUTPUT_INVALID",
        "EVIDENCE_BINDING_FAILED",
        "SEMANTIC_REVIEW_DEADLINE_EXHAUSTED",
        "SEMANTIC_REVIEW_NOT_AVAILABLE",
        "SEMANTIC_REVIEW_RESPONSE_INVALID",
    ),
)
def test_generation_validation_incomplete_is_not_provider_outage(
    reason: str,
) -> None:
    """成功响应后的生成或校验失败不应伪装成网络不可用。"""
    context = QueryDataPlaneContext(
        generation_provider_id="openai-compatible",
        generation_model="synthetic-model",
        model_configuration_state="CONFIGURED",
    )

    assert _model_capability_status(context, reason) is None


def test_review_model_identity_is_policy_blocker() -> None:
    context = QueryDataPlaneContext(
        generation_provider_id="openai-compatible",
        generation_model="synthetic-model",
        model_configuration_state="CONFIGURED",
    )

    assert _model_capability_status(
        context, "SEMANTIC_REVIEW_MODEL_IDENTITY_REQUIRED"
    ) == (
        ConfidenceStatus.POLICY_DENIED,
        "SEMANTIC_REVIEW_MODEL_IDENTITY_REQUIRED",
    )


def test_formal_span_recheck_accepts_trimmed_source_coordinates() -> None:
    """表格单元格去尾空白后同步缩短来源范围，不误报索引损坏。"""
    ranked = make_ranked_chunk(1, "QVK（Qua ")
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
        citation_text="QVK（Qua",
        source_label="公开回归.docx",
        source_spans=(span,),
    )

    assert _formal_span_is_current(chunk, original, span, item)


def test_formal_span_recheck_accepts_exact_derived_numbering() -> None:
    """无正文坐标的派生编号保留制表符时仍与入选证据合同一致。"""
    ranked = make_ranked_chunk(4, "\uf0fc\t")
    chunk = ranked.hydrated.chunk
    original = chunk.source_spans[0].model_copy(
        update={
            "span_type": SourceSpanKind.DERIVED_NUMBERING,
            "source_start_char": None,
            "source_end_char": None,
        }
    )
    chunk = chunk.model_copy(update={"source_spans": (original,)})
    span = original.model_copy(
        update={"chunk_start_char": 0, "chunk_end_char": 2}
    )
    item = EvidenceItem(
        evidence_id="S1",
        chunk_id=chunk.chunk_id,
        citation_text="\uf0fc\t",
        source_label="派生编号回归.docx",
        source_spans=(span,),
    )

    assert _formal_span_is_current(chunk, original, span, item)


def test_formal_span_recheck_accepts_long_source_subrange() -> None:
    """长段落中的连续逐字引用可按原文坐标通过复核。"""
    quote = "关键结论：切换前必须完成双人复核。"
    text = "背景说明。" * 80 + quote + "补充说明。" * 80
    chunk = make_ranked_chunk(2, text).hydrated.chunk
    original = chunk.source_spans[0]
    quote_start = text.index(quote)
    span = original.model_copy(
        update={
            "chunk_start_char": 0,
            "chunk_end_char": len(quote),
            "source_start_char": quote_start,
            "source_end_char": quote_start + len(quote),
        }
    )
    item = EvidenceItem(
        evidence_id="S1",
        chunk_id=chunk.chunk_id,
        citation_text=quote,
        source_label="长段落回归.docx",
        source_spans=(span,),
    )

    assert _formal_span_is_current(chunk, original, span, item)


def test_formal_span_recheck_rejects_invalid_long_source_subranges() -> None:
    """越界或来源身份变化的长段引用必须继续失败关闭。"""
    quote = "关键结论：切换前必须完成双人复核。"
    text = "背景说明。" * 80 + quote + "补充说明。" * 80
    chunk = make_ranked_chunk(3, text).hydrated.chunk
    original = chunk.source_spans[0]
    quote_start = text.index(quote)
    span = original.model_copy(
        update={
            "chunk_start_char": 0,
            "chunk_end_char": len(quote),
            "source_start_char": quote_start,
            "source_end_char": quote_start + len(quote),
        }
    )
    item = EvidenceItem(
        evidence_id="S1",
        chunk_id=chunk.chunk_id,
        citation_text=quote,
        source_label="长段落回归.docx",
        source_spans=(span,),
    )
    wrong_anchor = original.source_anchor.model_copy(
        update={"part_uri": "/word/header1.xml"}
    )
    wrong_structural_anchor = original.source_anchor.model_copy(
        update={"structural_path": ("body", "p:999")}
    )
    invalid_spans = {
        "out_of_bounds": span.model_copy(
            update={
                "source_start_char": len(text) - len(quote) + 1,
                "source_end_char": len(text) + 1,
            }
        ),
        "wrong_node": span.model_copy(update={"node_id": f"node_{'f' * 32}"}),
        "wrong_anchor": span.model_copy(update={"source_anchor": wrong_anchor}),
        "wrong_structure": span.model_copy(
            update={
                "source_anchor": wrong_structural_anchor,
                "structural_path": wrong_structural_anchor.structural_path,
            }
        ),
        "wrong_repeated_identity": span.model_copy(
            update={"is_repeated": True}
        ),
        "not_citable": span.model_copy(update={"is_citable": False}),
    }

    for reason, invalid_span in invalid_spans.items():
        invalid_item = item.model_copy(update={"source_spans": (invalid_span,)})
        assert not _formal_span_is_current(
            chunk, original, invalid_span, invalid_item
        ), reason

    forged = item.model_copy(
        update={"citation_text": quote.replace("必须", "无需")}
    )
    assert not _formal_span_is_current(chunk, original, span, forged)
