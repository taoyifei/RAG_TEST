"""请求来源的不可变内部传递合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

USAGE_SCHEMA_VERSION = "wst-usage-v1"
TrafficClass = Literal["INTERACTIVE", "EVALUATION", "SYSTEM", "LEGACY_UNKNOWN"]
Entrypoint = Literal["manual", "suggestion", "popular", "retry", "unknown"]


@dataclass(frozen=True, slots=True)
class QueryAuditContext:
    """认证后冻结、跨 worker 显式传递的单请求审计信息。"""

    trace_id: str
    project_id: str
    knowledge_base_id: str
    owner_id: str
    deployment_id: str | None
    identity_source: str
    traffic_class: TrafficClass
    classification_source: str
    entrypoint: Entrypoint = "unknown"
    entrypoint_source: str = "NO_HINT"
    recommendation_id: str | None = None
    hint_diagnostic: str | None = None

    def metadata(self, *, has_context: bool) -> dict[str, object]:
        """生成只含审计字段的独立元数据命名空间。"""
        result: dict[str, object] = {
            "schema_version": USAGE_SCHEMA_VERSION,
            "deployment_id": self.deployment_id,
            "traffic_class": self.traffic_class,
            "classification_source": self.classification_source,
            "identity_source": self.identity_source,
            "entrypoint": self.entrypoint,
            "entrypoint_source": self.entrypoint_source,
            "has_context": has_context,
        }
        if self.recommendation_id is not None:
            result["recommendation_id"] = self.recommendation_id
        if self.hint_diagnostic is not None:
            result["hint_diagnostic"] = self.hint_diagnostic
        return result
