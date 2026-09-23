"""问答 History 的可信来源审计合同。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rag_app.core.models.usage_audit import USAGE_SCHEMA_VERSION

_TRAFFIC_CLASSES = frozenset(
    {"INTERACTIVE", "EVALUATION", "SYSTEM", "LEGACY_UNKNOWN"}
)


@dataclass(frozen=True, slots=True)
class TrafficOverrideItem:
    """待修正 Trace 与调用方观察到的完整元数据版本。"""

    trace_id: str
    expected_metadata_revision: str


def metadata_revision(raw_metadata: str) -> str:
    """使 finish 与两次管理员修正都参与乐观锁比较。"""
    return hashlib.sha256(raw_metadata.encode("utf-8")).hexdigest()


def usage_audit_view(value: object) -> dict[str, object]:
    """投影当前有效分类，旧数据明确保持未知。"""
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != USAGE_SCHEMA_VERSION
    ):
        return {
            "schema_version": USAGE_SCHEMA_VERSION,
            "traffic_class": "LEGACY_UNKNOWN",
            "effective_traffic_class": "LEGACY_UNKNOWN",
            "classification_source": "LEGACY_UNKNOWN",
        }
    result = dict(value)
    captured = result.get("traffic_class")
    if not isinstance(captured, str) or captured not in _TRAFFIC_CLASSES:
        captured = "LEGACY_UNKNOWN"
    override = result.get("override")
    effective = (
        override.get("traffic_class") if isinstance(override, dict) else None
    )
    result["effective_traffic_class"] = (
        effective
        if isinstance(effective, str) and effective in _TRAFFIC_CLASSES
        else captured
    )
    return result
