"""只允许结构化、脱敏属性的 Trace 事件。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
    require_aware_datetime,
)


class TraceEvent(FrozenModel):
    """TracePort 接收的最小安全事件。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    event_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    occurred_at: datetime
    attributes: JsonObject = ()

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)

    @field_validator("attributes", mode="before")
    @classmethod
    def _freeze_attributes(cls, value: object) -> JsonObject:
        return freeze_json_object(value)
