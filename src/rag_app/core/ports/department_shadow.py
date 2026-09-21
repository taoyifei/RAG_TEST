"""部门影子路由的只读观察端口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from rag_app.core.models import EmbeddingSlotIdentity


@dataclass(frozen=True, slots=True)
class DepartmentShadowRequest:
    """主检索已经产生、可供本地观察器只读复用的输入。"""

    project_id: str
    knowledge_base_id: str
    index_revision_id: str
    resolved_root_query: str = field(repr=False)
    context_mode: str
    scope_digest: str
    actual_scope_kind: str
    explicit_source_document_ids: tuple[str, ...]
    final_cited_document_ids: tuple[str, ...]
    embedding_slot: EmbeddingSlotIdentity | None = None
    root_vector: tuple[float, ...] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DepartmentShadowObservation:
    """允许写入 SAFE Trace 的部门路由观察结果。"""

    route_revision: str
    profile_revision: str | None
    resolved_root_query_sha256: str
    context_mode: str
    scope_digest: str
    actual_scope_kind: str
    top1_department_key: str | None
    top1_score_bucket: str | None
    top2_department_key: str | None
    top2_score_bucket: str | None
    confidence: str
    recommended_scope: str
    final_cited_department_keys: tuple[str, ...]
    department_filter_applied: bool
    embedding_reused: bool
    extra_provider_calls: int
    status: str
    reason_codes: tuple[str, ...]
    elapsed_ms: int

    def trace_attributes(self) -> dict[str, object]:
        """投影为不含原问、向量或 Provider 配置的 Trace 字段。"""
        return {
            "route_revision": self.route_revision,
            "profile_revision": self.profile_revision,
            "resolved_root_query_sha256": (
                self.resolved_root_query_sha256
            ),
            "context_mode": self.context_mode,
            "scope_digest": self.scope_digest,
            "actual_scope_kind": self.actual_scope_kind,
            "top1_department_key": self.top1_department_key,
            "top1_score_bucket": self.top1_score_bucket,
            "top2_department_key": self.top2_department_key,
            "top2_score_bucket": self.top2_score_bucket,
            "confidence": self.confidence,
            "recommended_scope": self.recommended_scope,
            "final_cited_department_keys": (
                self.final_cited_department_keys
            ),
            "department_filter_applied": self.department_filter_applied,
            "embedding_reused": self.embedding_reused,
            "extra_provider_calls": self.extra_provider_calls,
            "status": self.status,
            "reason_codes": self.reason_codes,
            "elapsed_ms": self.elapsed_ms,
        }


class DepartmentShadowObserverPort(Protocol):
    """只计算观察建议，不得修改请求或触发外部调用。"""

    def observe(
        self, request: DepartmentShadowRequest
    ) -> DepartmentShadowObservation:
        """根据主链既有数据返回一个安全观察事件。"""
        ...


__all__ = [
    "DepartmentShadowObservation",
    "DepartmentShadowObserverPort",
    "DepartmentShadowRequest",
]
