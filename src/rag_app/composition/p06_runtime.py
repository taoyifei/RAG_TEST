"""P06 本地索引的单一组合根。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from rag_app.adapters.stores import (
    FilesystemBlobStore,
    SqliteConnectionFactory,
    SqliteControlStore,
    SqliteEmbeddingCache,
)
from rag_app.application.artifact_lifecycle import ArtifactLifecycleService
from rag_app.application.embedding_indexing import DocumentEmbeddingService
from rag_app.application.garbage_collection import GarbageCollector
from rag_app.application.revision_builder import RevisionBuilder
from rag_app.application.revision_recovery import RevisionRecoveryService
from rag_app.application.revision_validator import RevisionValidator
from rag_app.composition.factory import RagComponents, build_components
from rag_app.composition.profiles import RagProfile, load_profile
from rag_app.composition.registry import (
    ComponentRegistry,
    register_builtin_components,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import DocumentEmbeddingBudget
from rag_app.core.ports import ChunkValidationPort


@dataclass(slots=True)
class P06Runtime:
    """显式拥有 P06 控制面、构建器、恢复与 GC 服务。"""

    components: RagComponents
    control: SqliteControlStore
    cache: SqliteEmbeddingCache
    builder: RevisionBuilder
    validator: RevisionValidator
    recovery: RevisionRecoveryService
    garbage_collector: GarbageCollector
    database_identity: str
    connections: SqliteConnectionFactory

    def default_budgets(self) -> dict[str, DocumentEmbeddingBudget]:
        """返回足够本地开发、仍有硬上限的每槽预算。

        Args:
            无参数；读取 resolved topology。

        Returns:
            slot ID 到硬预算的映射。

        """
        return {
            slot.slot_id: DocumentEmbeddingBudget(
                max_requests=10000,
                max_tokens=10_000_000,
                max_chunks=1_000_000,
            )
            for slot in self.components.embedding_topology.slots
        }

    def close(self) -> None:
        """幂等关闭 cache 与组件集合。

        Args:
            无参数；关闭当前运行时。

        Returns:
            无返回值。

        """
        self.cache.close()
        self.components.close()

    def __enter__(self) -> P06Runtime:
        """进入资源作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开资源作用域。"""
        del args
        self.close()


def build_p06_runtime(
    profile: str | Path | RagProfile,
    *,
    data_dir: str | Path | None = None,
) -> P06Runtime:
    """构造无隐藏默认策略的 P06 本地运行时。

    Args:
        profile: 严格 Profile 或 JSON 文件路径。
        data_dir: 可选显式本地数据根覆盖。

    Returns:
        完整拥有资源的 P06 运行时。

    """
    resolved = (
        profile if isinstance(profile, RagProfile) else load_profile(profile)
    )
    if data_dir is not None:
        resolved = resolved.model_copy(
            update={
                "local_data": resolved.local_data.model_copy(
                    update={"data_root": str(data_dir)}
                )
            }
        )
    registry = ComponentRegistry()
    register_builtin_components(registry)
    components = build_components(resolved, registry)
    if not isinstance(components.metadata_store, SqliteControlStore):
        components.close()
        raise TypeError("P06 Profile 必须使用 sqlite-control。")
    if not isinstance(components.blob_store, FilesystemBlobStore):
        components.close()
        raise TypeError("P06 Profile 必须使用 filesystem-blob。")
    control = components.metadata_store
    control.recover_stale_jobs(
        (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    )
    data = resolved.local_data
    connections = SqliteConnectionFactory(
        Path(data.data_root) / data.sqlite_filename,
        busy_timeout_ms=data.busy_timeout_ms,
        journal_mode=data.journal_mode,
    )
    cache = SqliteEmbeddingCache(connections)
    providers = {
        components.embedding_topology.slots[
            0
        ].slot_id: components.embedding_primary
    }
    if components.embedding_standby is not None:
        providers[components.embedding_topology.slots[1].slot_id] = (
            components.embedding_standby
        )
    chunk_validator = getattr(components.chunker, "validate_persisted", None)
    if chunk_validator is None:
        components.close()
        raise TypeError("P06 Chunker 必须实现持久化 Chunk 校验端口。")
    validator = RevisionValidator(
        control,
        components.vector_store,
        cast(ChunkValidationPort, components.chunker),
    )
    embedding = DocumentEmbeddingService(cache, control, providers)
    builder = RevisionBuilder(
        control=control,
        parser=components.parser,
        parsing_policy=components.parsing_policy,
        chunker=components.chunker,
        chunking_policy=components.chunking_policy,
        artifact_lifecycle=ArtifactLifecycleService(
            components.blob_store,
            control,
            components.blob_store,
        ),
        embedding_service=embedding,
        embedding_providers=providers,
        vector_store=components.vector_store,
        validator=validator,
        slots=components.embedding_topology.slots,
        index_fingerprint=components.index_fingerprint,
        resolved_contracts=resolved_contracts(components),
    )
    database_identity = canonical_sha256(connections.database_identity())
    return P06Runtime(
        components=components,
        control=control,
        cache=cache,
        builder=builder,
        validator=validator,
        recovery=RevisionRecoveryService(control, components.vector_store),
        garbage_collector=GarbageCollector(
            control,
            components.vector_store,
            components.blob_store,
            database_identity=database_identity,
        ),
        database_identity=database_identity,
        connections=connections,
    )


def resolved_contracts(components: RagComponents) -> dict[str, object]:
    """导出不含 Secret 的实际 Revision 合同。

    Args:
        components: 已完成能力校验的组件集合。

    Returns:
        可持久化并供恢复与验证读取的合同映射。

    """
    descriptors = {
        item.kind.value: item.model_dump(mode="json")
        for item in components.descriptors
    }
    topology = components.embedding_topology
    return {
        "parser_identity": components.parser.descriptor.model_dump(mode="json"),
        "parsing_policy": components.parsing_policy.model_dump(mode="json"),
        "chunker_identity": components.chunker.descriptor.model_dump(
            mode="json"
        ),
        "chunking_policy": components.chunking_policy.model_dump(mode="json"),
        "embedding_topology": topology.model_dump(mode="json"),
        "lexical_schema": {
            "component": descriptors["lexical_store"],
            "fts_schema_version": "2",
            "analyzer_id": "deterministic-cjk-bigram",
            "analyzer_version": "2",
            "query_builder_version": "2",
        },
        "vector_schema": {
            "component": descriptors["vector_store"],
            "write_semantics": "complete-named-vector-point-v1",
            "slots": [slot.model_dump(mode="json") for slot in topology.slots],
        },
        "chunk_payload_schema": "canonical-chunk-v3",
        "serving_compatibility_version": "1",
    }


__all__ = ["P06Runtime", "build_p06_runtime", "resolved_contracts"]
