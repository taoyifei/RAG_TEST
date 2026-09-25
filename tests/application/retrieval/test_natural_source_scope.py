"""候选问答只在强语法或服务端选择时收窄文档版本。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.natural_source_scope import (
    needs_natural_source_catalog,
    resolve_natural_source_scope,
)
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


def test_within_quoted_document_requires_location_suffix() -> None:
    document = _document(1, "研发项目命名规范")

    hard = resolve_natural_source_scope(
        "在《研发项目命名规范》中有哪些要求？", (document,)
    )
    ordinary = resolve_natural_source_scope(
        "在《研发项目命名规范》发布后有哪些要求？", (document,)
    )

    assert hard.mode == "HARD_RESOLVED"
    assert ordinary.mode == "OPEN"


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
