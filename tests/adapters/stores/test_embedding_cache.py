from __future__ import annotations

from pathlib import Path

from rag_app.adapters.stores import (
    MigrationRunner,
    SqliteConnectionFactory,
    SqliteEmbeddingCache,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    CacheScope,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    EmbeddingRequestRole,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
)
from rag_app.core.models.common import freeze_json_object


def _identity(
    scope_id: str, *, adapter_revision: str = "1"
) -> EmbeddingCacheIdentity:
    return EmbeddingCacheIdentity(
        scope_kind=CacheScope.PROJECT,
        scope_id=scope_id,
        slot=EmbeddingSlotIdentity(
            slot_id="primary",
            role=EmbeddingSlotRole.PRIMARY,
            provider_id="deterministic",
            model="model-v1",
            vector_name="dense_primary",
            dimension=2,
            normalization="l2",
            adapter_revision=adapter_revision,
            document_request_policy=freeze_json_object({"role": "document"}),
            query_request_policy=freeze_json_object({"role": "query"}),
        ),
        role=EmbeddingRequestRole.DOCUMENT,
        role_policy_identity=canonical_sha256("document-policy"),
        text_sha256="a" * 64,
    )


def test_embedding_cache_is_ordered_scoped_and_persistent(
    tmp_path: Path,
) -> None:
    connections = SqliteConnectionFactory(tmp_path / "control.sqlite3")
    migrations = (
        Path(__file__).resolve().parents[3] / "migrations" / "universal_rag"
    )
    MigrationRunner(connections, migrations).migrate()
    cache = SqliteEmbeddingCache(connections)
    project_a = deterministic_id("prj", "a")
    project_b = deterministic_id("prj", "b")
    identity = _identity(project_a)
    record = EmbeddingCacheRecord(identity=identity, vector=(1.0, 0.5))
    cache.put_many((record,))

    assert cache.get_many((_identity(project_b), identity)) == (None, record)
    assert cache.get_many((_identity(project_a, adapter_revision="2"),)) == (
        None,
    )

    reopened = SqliteEmbeddingCache(connections)
    assert reopened.get_many((identity,)) == (record,)
