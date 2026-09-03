"""P06 persistence 与 P07 unified retrieval 的显式组合根。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rag_app.adapters.stores import InMemoryRetrievalCache, SqliteFtsStore
from rag_app.application.retrieval import RetrievalService
from rag_app.composition.p06_runtime import P06Runtime, build_p06_runtime
from rag_app.composition.profiles import RagProfile
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import RetrievalPolicy
from rag_app.core.ports import ExactStorePort


@dataclass(slots=True)
class P07Runtime:
    """显式拥有持久化 runtime、retrieval service 和进程内 cache。"""

    persistence: P06Runtime
    retrieval: RetrievalService
    cache: InMemoryRetrievalCache

    def close(self) -> None:
        """先清理可能含正文的 cache，再关闭 P06 资源。

        Args:
            无参数；关闭当前 runtime。

        Returns:
            无返回值。

        """
        self.cache.close()
        self.persistence.close()

    def __enter__(self) -> P07Runtime:
        """进入宿主管理的资源作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开资源作用域并幂等关闭。"""
        del args
        self.close()


def build_p07_runtime(
    profile: str | Path | RagProfile,
    *,
    data_dir: str | Path | None = None,
    policy: RetrievalPolicy | None = None,
) -> P07Runtime:
    """构造默认离线、同步且 legacy HTTP 未切换的 P07 runtime。

    Args:
        profile: 严格 Profile 或 JSON 文件路径。
        data_dir: 可选显式本地数据根。
        policy: 可选 P07 provisional 策略覆盖。

    Returns:
        持有 unified retrieval service 的 runtime。

    """
    persistence = build_p06_runtime(profile, data_dir=data_dir)
    components = persistence.components
    if not isinstance(components.lexical_store, SqliteFtsStore):
        persistence.close()
        raise TypeError("P07 Exact/FTS 查询要求 sqlite-fts5。")
    for revision_id in persistence.control.active_revision_ids():
        persistence.recovery.backfill(revision_id)
    cache = InMemoryRetrievalCache()
    serving_fingerprint = components.serving_fingerprint
    if policy is not None:
        serving_fingerprint = canonical_sha256(
            {
                "base_serving_fingerprint": serving_fingerprint,
                "retrieval_policy": policy.model_dump(mode="json"),
            }
        )
    retrieval = RetrievalService(
        source=persistence.control,
        exact_store=cast(ExactStorePort, components.lexical_store),
        lexical_store=components.lexical_store,
        vector_store=components.vector_store,
        query_embedding=components.query_embedding_router,
        reranker=components.reranker,
        generator=components.generator,
        trace=components.trace_sink,
        cache=cache,
        serving_fingerprint=serving_fingerprint,
        egress_policy=components.profile.security,
        policy=policy,
    )
    return P07Runtime(persistence=persistence, retrieval=retrieval, cache=cache)


__all__ = ["P07Runtime", "build_p07_runtime"]
