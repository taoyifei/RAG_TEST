"""完整请求的只读展示开关对照；所有输入均为 unit_synthetic。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import P07Runtime, build_p07_runtime
from rag_app.core.errors import (
    IndexCorrupt,
    ProviderAuthenticationError,
    ProviderUnavailable,
)
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChannelHit,
    ConfidenceStatus,
    DocumentRef,
    KnowledgeBaseScope,
    SearchAnswerResult,
    SearchRequest,
)
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.support.p11_closure_replay import no_network

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PARAGRAPHS = (
    "隐私专员负责受理申请。",
    "冷却器数量为12台，温度为8℃。",
    "阅览厅长度为20米，位于3楼。",
    "值班员联系电话为010-87654321。",
    "驱动器售价为35美元。",
    "实验楼面积为125.5平方米。",
    "订单 ABC-123。",
    "个人信息更正申请由隐私专员受理。",
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[P07Runtime]:
    project_id = deterministic_id("prj", "related-service")
    kb_id = deterministic_id("kb", "related-service")
    with no_network(), build_p07_runtime(_PROFILE, data_dir=tmp_path) as value:
        control = value.persistence.control
        control.put_project(project_id, "Synthetic")
        control.put_knowledge_base(
            kb_id, project_id, "Synthetic", profile_id="dev-p06-memory"
        )
        value.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=kb_id,
            documents=tuple(
                IngestionDocument(
                    document=DocumentRef(
                        project_id=project_id,
                        knowledge_base_id=kb_id,
                        document_id=deterministic_id("doc", str(index)),
                        display_name=f"source-{index}.docx",
                    ),
                    content=build_docx(
                        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
                    ),
                    media_type=_MEDIA_TYPE,
                )
                for index, text in enumerate(_PARAGRAPHS)
            ),
            idempotency_key="related-service",
            budgets=value.persistence.default_budgets(),
        )
        yield value


def _request(query: str, *, related: bool = False) -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "related-service"),
            knowledge_base_id=deterministic_id("kb", "related-service"),
        ),
        text=query,
        include_related_content=related,
    )


@pytest.fixture
def lexical_candidates(runtime: P07Runtime) -> Iterator[None]:
    """注入明确的 lexical 身份，单独验证已有回答资格与展示隔离。

    默认离线 embedding 未校准，不能把仅 Dense 的属性回答当作正例。
    此合成通道不宣称 FTS 召回质量；canonical 和后续全链路仍真实执行。
    """
    scope = _request("synthetic").scope
    control = runtime.persistence.control
    revision_id = control.active_revision_ids()[0]
    chunks, _ = control.list_revision_chunks(
        scope.project_id,
        scope.knowledge_base_id,
        revision_id,
        document_id=None,
        role=None,
        section_id=None,
        neighbor_group_id=None,
        limit=20,
        offset=0,
    )
    hits = tuple(
        ChannelHit(
            revision_id=revision_id,
            chunk_id=chunk.chunk_id,
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
            role=chunk.role.value,
            section_id=chunk.section_id,
            content_sha256=chunk.content_sha256,
            channel="lexical:fts5",
            rank=index,
            raw_score=1.0,
        )
        for index, chunk in enumerate(chunks, 1)
    )
    with patch.object(runtime.retrieval._lexical, "search", return_value=hits):
        yield


@pytest.mark.parametrize(
    "query,answerable",
    (
        ("隐私专员联系电话是多少？", False),
        ("冷却器采购价格是多少？", False),
        ("阅览厅建筑面积是多少？", False),
        ("值班员联系电话是多少？", True),
        ("驱动器售价多少？", True),
        ("实验楼面积多大？", True),
        ("个人信息更正申请由谁受理？", True),
        ("ABC-123", True),
    ),
)
@pytest.mark.usefixtures("lexical_candidates")
def test_switch_preserves_formal_contract_and_provider_calls(
    runtime: P07Runtime,
    query: str,
    answerable: bool,
) -> None:
    components = runtime.persistence.components
    with (
        patch.object(
            components.query_embedding_router,
            "embed_query",
            wraps=components.query_embedding_router.embed_query,
        ) as embedding,
        patch.object(
            components.reranker, "rerank", wraps=components.reranker.rerank
        ) as rerank,
        patch.object(
            components.generator,
            "generate",
            wraps=components.generator.generate,
        ) as generator,
    ):
        plain = runtime.retrieval.search_and_answer(_request(query))
        first_counts = (
            embedding.call_count,
            rerank.call_count,
            generator.call_count,
        )
        shown = runtime.retrieval.search_and_answer(
            _request(query, related=True)
        )
        assert (
            embedding.call_count,
            rerank.call_count,
            generator.call_count,
        ) == tuple(n * 2 for n in first_counts)
        assert shown.status == plain.status
        assert shown.answer == plain.answer
        assert shown.confidence == plain.confidence
        assert shown.evidence == plain.evidence
        assert shown.cache_key != plain.cache_key
        assert (shown.status is ConfidenceStatus.ANSWERABLE) == answerable
        assert plain.related_contents == ()
        if answerable:
            assert shown.answer and not shown.related_contents
        else:
            assert shown.answer is None
            assert 1 <= len(shown.related_contents) <= 3
            assert (
                shown.display_message
                and "本次检索未找到足以直接回答" in shown.display_message
            )
        for call in generator.call_args_list:
            assert "related_contents" not in repr(call)


def test_legacy_result_defaults_and_cache_rechecks_deleted_document(
    runtime: P07Runtime,
) -> None:
    request = _request("隐私专员联系电话是多少？", related=True)
    first = runtime.retrieval.search_and_answer(request)
    assert first.related_contents
    second = runtime.retrieval.search_and_answer(request)
    assert second.cache_hit
    legacy = first.model_dump(exclude={"related_contents", "display_message"})
    assert SearchAnswerResult.model_validate(legacy).related_contents == ()
    with runtime.persistence.control._connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE documents SET status='deleted', deleted_at='synthetic' "
            "WHERE document_id=?",
            (first.related_contents[0].document_id,),
        )
    with pytest.raises(IndexCorrupt):
        runtime.retrieval.search_and_answer(request)


@pytest.mark.usefixtures("lexical_candidates")
def test_rerank_failure_keeps_lexical_answer_and_is_not_cached(
    runtime: P07Runtime,
) -> None:
    components = runtime.persistence.components
    request = _request("个人信息更正申请由谁受理？", related=True)
    with patch.object(
        components.reranker,
        "rerank",
        side_effect=ProviderUnavailable("unit_synthetic", stage="test"),
    ):
        degraded = runtime.retrieval.search_and_answer(request)
    assert degraded.status is ConfidenceStatus.ANSWERABLE
    assert not degraded.related_contents and degraded.display_message is None
    recovered = runtime.retrieval.search_and_answer(request)
    assert recovered.status is ConfidenceStatus.ANSWERABLE
    assert not recovered.cache_hit
    assert recovered.rerank_execution_mode != degraded.rerank_execution_mode


def test_real_failure_category_controls_notice_without_changing_refusal(
    runtime: P07Runtime,
) -> None:
    request = _request("值班员联系电话是多少？", related=True)
    with patch.object(
        runtime.persistence.components.reranker,
        "rerank",
        side_effect=ProviderUnavailable("unit_synthetic", stage="test"),
    ):
        result = runtime.retrieval.search_and_answer(request)
    assert result.answer is None and result.related_contents
    assert (
        result.display_message
        and "重排服务暂时不可用" in result.display_message
    )
    assert all(not item.rerank_verified for item in result.related_contents)
    assert runtime.cache.get(result.cache_key) is None


def test_provider_auth_failure_and_its_circuit_are_not_service_outage(
    runtime: P07Runtime,
) -> None:
    request = _request("值班员联系电话是多少？", related=True)
    with patch.object(
        runtime.persistence.components.reranker,
        "rerank",
        side_effect=ProviderAuthenticationError("unit_synthetic", stage="test"),
    ) as spy:
        first = runtime.retrieval.search_and_answer(request)
        second = runtime.retrieval.search_and_answer(request)
    assert spy.call_count == 1
    for result in (first, second):
        assert result.answer is None
        assert (
            result.display_message
            and "服务暂时不可用" not in result.display_message
        )
        assert runtime.cache.get(result.cache_key) is None


def test_empty_allowed_documents_excludes_all_channels(
    runtime: P07Runtime,
) -> None:
    request = _request("值班员联系电话是多少？", related=True).model_copy(
        update={
            "access_filters": (("allowed_document_ids", ()),),
        }
    )
    result = runtime.retrieval.search_and_answer(request)
    assert result.answer is None
    assert not result.evidence and not result.related_contents
    assert (
        result.display_message
        and "本次检索未找到足够相关的内容" in result.display_message
    )


def test_formal_cache_preserves_relative_spans_and_rejects_forged_quote(
    runtime: P07Runtime,
) -> None:
    request = _request("ABC-456")
    runtime.persistence.builder.build_and_activate(
        project_id=request.scope.project_id,
        knowledge_base_id=request.scope.knowledge_base_id,
        documents=(
            IngestionDocument(
                document=DocumentRef(
                    project_id=request.scope.project_id,
                    knowledge_base_id=request.scope.knowledge_base_id,
                    document_id=deterministic_id("doc", "multi-span-cache"),
                    display_name="multi-span.docx",
                ),
                content=build_docx(
                    "<w:p><w:r><w:t>这里是背景介绍。</w:t></w:r></w:p>"
                    "<w:p><w:r><w:t>订单编号 ABC-456。</w:t></w:r></w:p>"
                ),
                media_type=_MEDIA_TYPE,
            ),
        ),
        idempotency_key="multi-span-cache",
        budgets=runtime.persistence.default_budgets(),
    )
    first = runtime.retrieval.search_and_answer(request)
    assert first.answer and first.evidence
    assert first.evidence[0].source_spans[0].chunk_start_char == 0
    cached = runtime.retrieval.search_and_answer(request)
    assert cached.cache_hit and cached.evidence == first.evidence
    forged = first.model_copy(
        update={
            "evidence": (
                first.evidence[0].model_copy(
                    update={"citation_text": "伪造的订单 ABC-456。"}
                ),
            )
        }
    )
    runtime.cache.put(first.cache_key, forged, ttl_seconds=30)
    with pytest.raises(IndexCorrupt):
        runtime.retrieval.search_and_answer(request)
