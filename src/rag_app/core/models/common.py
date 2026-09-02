"""Core 模型共享的严格、不可变基础类型。"""

from __future__ import annotations

from pydantic import Field, field_validator

from rag_app.core._base import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
    require_aware_datetime,
)

__all__ = [
    "FrozenModel",
    "JsonObject",
    "MetadataModel",
    "SecretRef",
    "freeze_json_object",
    "require_aware_datetime",
]


class SecretRef(FrozenModel):
    """只引用环境变量名，不持有 secret 值。"""

    env_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")


class MetadataModel(FrozenModel):
    """为携带有限 JSON metadata 的模型提供统一校验。"""

    metadata: JsonObject = ()

    @field_validator("metadata", mode="before")
    @classmethod
    def _freeze_metadata(cls, value: object) -> JsonObject:
        return freeze_json_object(value)
