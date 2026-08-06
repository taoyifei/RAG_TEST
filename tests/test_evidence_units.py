import json

from rag_app.chunking import Utf8TokenCounter
from rag_app.generation.evidence import (
    AnswerabilityStatus,
    EvidenceAssembler,
    EvidenceBundle,
    EvidenceConfig,
    decide_answerability,
)
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.rerank import RerankedHit


def _ranked(  # noqa: PLR0913
    chunk_id: str,
    text: str,
    *,
    rank: int = 1,
    score: float,
    file_path: str = "研发规范.docx",
    source_id: str = "source-1",
    neighbor_group_id: str = "neighbor-1",
) -> RerankedHit:
    locator = {
        "file_path": file_path,
        "heading_path": ["需求管理", "变更管理"],
        "paragraph_index": 1,
        "fragment": text[:40],
    }
    return RerankedHit(
        rank=rank,
        rerank_score=score,
        hit=FusedHit(
            chunk_id=chunk_id,
            rrf_score=0.1,
            channel_ranks=(("q0:dense", 1),),
            payload={
                "chunk_id": chunk_id,
                "source_id": source_id,
                "neighbor_group_id": neighbor_group_id,
                "text": text,
                "embedding_text": text,
                "locators": [locator],
                "source_spans": [
                    {
                        "element_id": f"element-{chunk_id}",
                        "locator": locator,
                        "start_char": 0,
                        "end_char": len(text),
                        "source_start_char": 0,
                        "source_end_char": len(text),
                        "is_repeated": False,
                    }
                ],
                "contains_ocr": False,
                "minimum_ocr_confidence": None,
            },
        ),
    )


def _assemble(*hits: RerankedHit) -> EvidenceBundle:
    return EvidenceAssembler(
        Utf8TokenCounter(),
        EvidenceConfig(
            max_evidence_tokens=4096,
            max_items=8,
            low_ocr_threshold=0.8,
        ),
    ).assemble(tuple(hits))


def test_evidence_units_are_exact_atomic_spans_without_internal_locators(
) -> None:
    text = "提交书面申请；完成影响评估；双方书面确认后更新需求基线。"

    bundle = _assemble(_ranked("chunk-1", text, score=1.0))

    assert [unit.unit_id for unit in bundle.units] == [
        "E1:S1",
        "E1:S2",
        "E1:S3",
    ]
    assert "".join(unit.text for unit in bundle.units) == text
    assert all(unit.evidence_id == "E1" for unit in bundle.units)
    assert len({unit.source_group for unit in bundle.units}) == 1
    assert all(unit.chunk_id == "chunk-1" for unit in bundle.units)
    assert all(unit.rerank_rank == 1 for unit in bundle.units)
    assert all(unit.rerank_score == 1.0 for unit in bundle.units)
    assert all(
        unit.locator.file_path == "研发规范.docx"
        for unit in bundle.units
    )

    prompt_payload = json.loads(bundle.rendered_json)
    assert prompt_payload["evidence_units"][0] == {
        "low_confidence_ocr": False,
        "source_group": bundle.units[0].source_group,
        "source_label": "研发规范.docx > 需求管理 > 变更管理",
        "text": "提交书面申请；",
        "unit_id": "E1:S1",
    }
    assert "chunk_id" not in bundle.rendered_json
    assert "locator" not in bundle.rendered_json
    assert "source_spans" not in bundle.rendered_json


def test_answerability_uses_trace_calibrated_gap_and_anchor_coverage() -> None:
    answerable = _assemble(
        _ranked(
            "chunk-answerable",
            "需求变更应提交申请并完成影响评估。",
            score=0.9961,
        )
    )
    missing = _assemble(
        _ranked(
            "chunk-unrelated",
            "验收测试应记录测试结论。",
            score=0.3477,
        )
    )

    supported = decide_answerability(
        "需求变更需要经过哪些审批步骤？",
        answerable,
        rerank_scores=(0.9961, 0.9922, 0.9844),
    )
    not_found = decide_answerability(
        "知识库是否记载火星基地RAG-999项目在2099年的负责人？",
        missing,
        rerank_scores=(0.3477, 0.0447, 0.0275),
    )

    assert supported.status is AnswerabilityStatus.SUPPORTED
    assert supported.top_score == 0.9961
    assert not_found.status is AnswerabilityStatus.NOT_FOUND
    assert not_found.strong_anchor_count >= 2
    assert not_found.covered_anchor_count == 0
    assert not_found.non_low_ocr_evidence_count == 1


def test_low_score_without_strong_anchor_remains_ambiguous() -> None:
    bundle = _assemble(
        _ranked("chunk-1", "一般管理要求。", score=0.3477)
    )

    decision = decide_answerability(
        "有哪些管理要求？",
        bundle,
        rerank_scores=(0.3477, 0.0447),
    )

    assert decision.status is AnswerabilityStatus.AMBIGUOUS
