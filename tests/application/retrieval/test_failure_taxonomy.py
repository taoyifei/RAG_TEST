from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.stores import SqliteFtsStore
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.errors import ChannelUnavailable, IndexCorrupt
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from tests.e2e.test_p07_retrieval import _build_active_revision

_PROFILE = Path("configs/profiles/dev-p06-memory.json")


def test_declared_transient_channel_failure_can_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, knowledge_base_id, _ = _build_active_revision(tmp_path)
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )

    def unavailable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ChannelUnavailable("injected", stage="test")

    monkeypatch.setattr(SqliteFtsStore, "search_candidates", unavailable)
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        result = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )

    assert "CHANNEL_UNAVAILABLE" in result.degraded_reason_codes


def test_programming_error_is_not_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, knowledge_base_id, _ = _build_active_revision(tmp_path)
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )

    def broken(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected programming defect")

    monkeypatch.setattr(SqliteFtsStore, "search_candidates", broken)
    with (
        build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime,
        pytest.raises(RuntimeError, match="programming defect"),
    ):
        runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )


def test_broken_fts_schema_is_index_corrupt(tmp_path: Path) -> None:
    project_id, knowledge_base_id, _ = _build_active_revision(tmp_path)
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        connections = runtime.persistence.control._connections
        with connections.transaction(write=True) as connection:
            connection.execute("DROP TABLE chunks_fts_v2")
        with pytest.raises(IndexCorrupt):
            runtime.retrieval.search_and_answer(
                SearchRequest(scope=scope, text="A B")
            )


def test_malformed_canonical_chunk_json_is_index_corrupt(
    tmp_path: Path,
) -> None:
    project_id, knowledge_base_id, revision_id = _build_active_revision(
        tmp_path
    )
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        connections = runtime.persistence.control._connections
        with connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE chunks SET chunk_json='{}' WHERE revision_id=?",
                (revision_id,),
            )
        with pytest.raises(IndexCorrupt):
            runtime.retrieval.search_and_answer(
                SearchRequest(scope=scope, text="ABC-123")
            )
