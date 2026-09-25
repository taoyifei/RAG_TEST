"""候选问答只在强语法或服务端选择时收窄文档版本。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.natural_source_scope import (
    needs_natural_source_catalog,
    resolve_natural_source_scope,
)
from rag_app.application.retrieval.reranking import RerankingOutcome
from rag_app.application.retrieval.weknora_pipeline import (
    WeKnoraStandardPipeline,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import PolicyDenied
from rag_app.core.models.query_plan import SourceDocumentIdentity
from rag_app.core.ports.evidence_source import CatalogDocument
from tests.application.retrieval.test_weknora_pipeline import (
    _request,
    _scenario,
)


def _document(number: int, title: str) -> CatalogDocument:
    return CatalogDocument(
        document_id=f"doc_{number:032x}",
        document_version_id=f"dver_{number:032x}",
        chunk_id=f"chunk_{number:032x}",
        title=title,
        metadata=(),
    )


@pytest.mark.parametrize(
    "query",
    (
        "研发项目命名规范中，研发项目名称由哪些要素组成？",
        "制度里有哪些要求？",
    ),
)
def test_weak_source_phrase_stays_open_to_authorized_kb(query: str) -> None:
    decision = resolve_natural_source_scope(query, ())

    assert decision.mode in {"SOFT_HINT", "OPEN"}
    assert decision.allowed_documents == ()
    assert not needs_natural_source_catalog(query)


def test_reported_p0_reaches_real_retrieval() -> None:
    service, _model = _scenario()
    request = _request().model_copy(
        update={"text": "研发项目命名规范中，研发项目名称由哪些要素组成？"}
    )

    WeKnoraStandardPipeline(service).run(
        request,
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    service._lexical.search.assert_called_once()
    assert any(
        call.args[1] == "weknora_source_scope"
        and call.args[2]["scope_mode"] == "SOFT_HINT"
        for call in service._record.call_args_list
    )


def test_quoted_explicit_scope_is_exact_and_versioned() -> None:
    document = _document(1, "研发项目命名规范")
    question = "根据《研发项目命名规范》，名称由哪些要素组成？"
    decision = resolve_natural_source_scope(question, (document,))

    assert needs_natural_source_catalog(question)
    assert decision.mode == "HARD_RESOLVED"
    assert decision.allowed_documents == (
        SourceDocumentIdentity(
            document_id=document.document_id,
            document_version_id=document.document_version_id,
        ),
    )


def test_quoted_document_is_bound_without_a_prefix_trigger() -> None:
    document = _document(1, "研发项目命名规范")

    hard = resolve_natural_source_scope(
        "在《研发项目命名规范》中有哪些要求？", (document,)
    )
    ordinary = resolve_natural_source_scope(
        "在《研发项目命名规范》发布后有哪些要求？", (document,)
    )

    assert hard.mode == "HARD_RESOLVED"
    assert ordinary.mode == "HARD_RESOLVED"


def test_user_released_source_opens_scope_without_erasing_selection() -> None:
    document = _document(1, "研发项目命名规范")
    question = "根据《研发项目命名规范》说明要求。"

    assert not needs_natural_source_catalog(
        question, ignore_mentioned_sources=True
    )
    released = resolve_natural_source_scope(
        question, (document,), ignore_mentioned_sources=True
    )
    selected = resolve_natural_source_scope(
        question,
        (document,),
        selected_documents=(
            SourceDocumentIdentity(
                document_id=document.document_id,
                document_version_id=document.document_version_id,
            ),
        ),
        ignore_mentioned_sources=True,
    )

    assert released.mode == "OPEN"
    assert released.trigger == "USER_RELEASED"
    assert selected.mode == "HARD_RESOLVED"
    assert needs_natural_source_catalog(
        question, selected=True, ignore_mentioned_sources=True
    )


def test_ab019_template_scope_reaches_all_retrieval_channels() -> None:
    base = _document(2, "ignored")
    document = CatalogDocument(
        document_id=base.document_id,
        document_version_id=base.document_version_id,
        chunk_id=base.chunk_id,
        title="1-项目启动-XXX项目软件研发管理计划20260612.docx",
        metadata=(("trusted_aliases", ("研发管理计划",)),),
    )
    other = _document(1, "研发项目命名规范")
    question = "仅看《研发管理计划》模板，项目范围应如何写清边界？"
    service, _model = _scenario()
    service._source_catalog_context.return_value = (
        (document, other),
        True,
        "sha256:" + "3" * 64,
    )
    service._reranker.rerank.return_value = RerankingOutcome(
        candidates=(), mode="bypass", reason_code="NO_CANDIDATES"
    )
    request = _request().model_copy(update={"text": question})

    result = WeKnoraStandardPipeline(service).run(
        request,
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
        rewrite_enabled=False,
    )

    assert needs_natural_source_catalog(question)
    assert result.reason_code == "NO_RETRIEVAL_MATERIAL"
    allowed = service._lexical.search.call_args.kwargs["allowed_documents"]
    assert allowed == (
        SourceDocumentIdentity(
            document_id=document.document_id,
            document_version_id=document.document_version_id,
        ),
    )
    assert service._hydrator.hydrate.call_args.args[1] == ()
    assert any(
        call.args[1] == "weknora_source_scope"
        and call.args[2]["scope_mode"] == "HARD_RESOLVED"
        for call in service._record.call_args_list
    )


def test_real_style_title_without_alias_needs_clarification() -> None:
    document = _document(2, "1-项目启动-XXX项目软件研发管理计划20260612.docx")
    decision = resolve_natural_source_scope(
        "仅看《研发管理计划》模板，如何写目标？", (document,)
    )

    assert decision.mode == "HARD_UNRESOLVED"
    assert decision.resolution == "UNRESOLVED"


def test_quoted_document_in_middle_requires_unique_catalog_identity() -> None:
    document = _document(1, "研发管理计划")
    question = "请先说明范围，然后依据《研发管理计划》回答目标。"
    assert resolve_natural_source_scope(question, (document,)).mode == (
        "HARD_RESOLVED"
    )
    assert resolve_natural_source_scope(question, ()).mode == "HARD_UNRESOLVED"


@pytest.mark.parametrize(
    "documents",
    ((), (_document(1, "同名规范"), _document(2, "同名规范"))),
)
def test_quoted_missing_or_duplicate_fails_closed(
    documents: tuple[CatalogDocument, ...],
) -> None:
    decision = resolve_natural_source_scope(
        "根据《同名规范》说明要求", documents
    )

    assert decision.mode == "HARD_UNRESOLVED"
    assert not decision.allowed_documents


def test_unquoted_prefix_requires_unique_exact_catalog_match() -> None:
    query = "根据研发项目命名规范中，名称由哪些要素组成？"
    matched = resolve_natural_source_scope(
        query, (_document(1, "研发项目命名规范"),)
    )
    unmatched = resolve_natural_source_scope(query, ())

    assert matched.mode == "HARD_RESOLVED"
    assert unmatched.mode == "SOFT_HINT"


def test_server_selection_cannot_use_invisible_document_version() -> None:
    document = _document(1, "研发项目命名规范")
    selected = SourceDocumentIdentity(
        document_id=document.document_id,
        document_version_id=_document(2, "旧版").document_version_id,
    )

    decision = resolve_natural_source_scope(
        "名称由哪些要素组成？",
        (document,),
        selected_documents=(selected,),
    )

    assert decision.mode == "HARD_UNRESOLVED"
    assert not decision.allowed_documents


def test_explicit_missing_document_stops_before_retrieval() -> None:
    service, _model = _scenario()
    request = _request().model_copy(
        update={"text": "根据《不存在的规范》说明要求"}
    )

    with pytest.raises(PolicyDenied, match="SOURCE_SCOPE_NOT_RESOLVED"):
        WeKnoraStandardPipeline(service).run(
            request,
            engine_id="wk-standard-v1",
            cancellation=StreamCancellation(),
        )

    service._lexical.search.assert_not_called()
