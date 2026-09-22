"""Chunk V3 索引不变时的有界重排上下文测试。"""

from __future__ import annotations

from rag_app.application.retrieval.contextual_text import contextual_rerank_text
from rag_app.core.models.common import freeze_json_object
from tests.application.retrieval.helpers import make_ranked_chunk


def test_rerank_context_uses_trusted_metadata_without_reindexing() -> None:
    candidate = make_ranked_chunk(1, "申请人提交材料。")
    original = candidate.hydrated.chunk
    chunk = original.model_copy(
        update={
            "heading_path": ("Ａ类", "Ａ类", "办理流程"),
            "metadata": freeze_json_object(
                {
                    "document_title": "  申报指南  ",
                    "department_name": " 公共服务部 ",
                    "category_path": ["办事", "办事", "材料办理"],
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )

    views = contextual_rerank_text(candidate)

    assert views.rerank_text.startswith(
        "fixture.docx\nA类 / 办理流程\n申请人提交材料。"
    )
    assert views.rerank_text.index(original.citation_text) < (
        views.rerank_text.index("部门：公共服务部")
    )
    assert "分类：办事 > 材料办理" in views.rerank_text
    assert "结构：TEXT" in views.rerank_text
    assert original.citation_text in views.rerank_text
    assert views.embedding_text_override is None
    assert views.lexical_text_override is None
    assert chunk.embedding_text == original.embedding_text
    assert chunk.lexical_text == original.lexical_text
    assert len(views.rerank_text) - len(original.citation_text) <= 602


def test_title_already_at_start_of_body_is_not_duplicated() -> None:
    candidate = make_ranked_chunk(2, "申报指南说明申请条件。")
    chunk = candidate.hydrated.chunk.model_copy(
        update={"metadata": freeze_json_object({"document_title": "申报指南"})}
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={"chunk": chunk, "display_name": "申报指南"}
            )
        }
    )

    views = contextual_rerank_text(candidate)

    assert not views.rerank_text.startswith("申报指南\n")
    assert chunk.citation_text in views.rerank_text
