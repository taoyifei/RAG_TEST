from __future__ import annotations

from pathlib import Path

from evaluation.v2.dataset import load_dataset_directory
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from tests.e2e.test_p07_retrieval import _build_active_revision

_PROFILE = Path("configs/profiles/dev-p06-memory.json")


def test_p08_5_diagnostics_are_stage_specific_and_public_safe(
    tmp_path: Path,
) -> None:
    project_id, knowledge_base_id, _ = _build_active_revision(tmp_path)
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        result = runtime.retrieval.search_and_answer(
            SearchRequest(
                scope=KnowledgeBaseScope(
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                ),
                text="ABC-123",
            )
        )

    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.channel_chunk_ids
    assert diagnostics.fused_chunk_ids
    assert diagnostics.reranked
    assert diagnostics.evidence
    assert result.diagnostics_summary is not None
    assert result.diagnostics_summary.provider_call_count == sum(
        item.call_count for item in diagnostics.provider_calls
    )
    serialized = result.model_dump_json()
    assert "diagnostics\"" not in serialized
    assert "ABC-123" not in diagnostics.model_dump_json()


def test_p08_5_dataset_and_schema_migrations_are_complete(
    tmp_path: Path,
) -> None:
    dataset = load_dataset_directory(Path("evaluation/datasets/synthetic"))
    with (
        build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime,
        runtime.persistence.control._connections.transaction() as connection,
    ):
        migrations = tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        )

    assert len(dataset.cases) >= 50
    assert migrations[5:9] == (6, 7, 8, 9)
    assert migrations[-1] == 17
