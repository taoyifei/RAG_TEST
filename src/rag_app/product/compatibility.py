"""产品 Runtime 的兼容性清单读取与生成。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rag_app._build_revision import SOURCE_REVISION

CURRENT_DATABASE_SCHEMA = 17


class SchemaRange(BaseModel):
    """应用接受的连续数据库 Migration 范围。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum: int = Field(ge=1)
    maximum: int = Field(ge=1)


class CompatibilityManifest(BaseModel):
    """把可追溯身份与启动兼容性判断分离。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    application_version: str
    source_revision: str
    frontend_build_id: str
    database_schema_range: SchemaRange
    ir_schema: str
    chunk_schema: str
    fts_schema: str
    provider_adapter_versions: dict[str, str]
    tested_qdrant_range: str

    def require_compatible(self) -> None:
        """只验证行为兼容性，不要求 Git SHA 完全一致。

        Args:
            无参数；读取当前 Manifest。

        Returns:
            兼容时无返回值。

        Raises:
            ValueError: 数据库或核心 schema 不兼容。

        """
        schema_range = self.database_schema_range
        if (
            not schema_range.minimum
            <= CURRENT_DATABASE_SCHEMA
            <= schema_range.maximum
        ):
            raise ValueError("数据库 Migration 不在应用兼容范围内。")
        expected = ("document-ir-v4", "canonical-chunk-v3", "fts-v2")
        observed = (self.ir_schema, self.chunk_schema, self.fts_schema)
        if observed != expected:
            raise ValueError(
                "IR、Chunk 或 FTS schema 与 Product Runtime 不兼容。"
            )


def default_manifest() -> CompatibilityManifest:
    """生成当前源码的默认兼容性清单。

    Args:
        无参数；读取构建身份和固定适配器版本。

    Returns:
        当前应用兼容性清单。

    """
    return CompatibilityManifest(
        application_version="0.1.0",
        source_revision=SOURCE_REVISION,
        frontend_build_id="universal-rag-console@0.1.0",
        database_schema_range=SchemaRange(
            minimum=CURRENT_DATABASE_SCHEMA, maximum=CURRENT_DATABASE_SCHEMA
        ),
        ir_schema="document-ir-v4",
        chunk_schema="canonical-chunk-v3",
        fts_schema="fts-v2",
        provider_adapter_versions={
            "aliyun-model-studio": "1",
            "jina": "1",
        },
        tested_qdrant_range=">=1.18,<2",
    )


def load_manifest(path: str | Path | None) -> CompatibilityManifest:
    """读取显式清单或使用当前构建默认值。

    Args:
        path: 可选 JSON 清单路径。

    Returns:
        已验证且兼容的 Manifest。

    Raises:
        ValueError: JSON 或兼容范围无效。

    """
    if path is None:
        manifest = default_manifest()
    else:
        manifest = CompatibilityManifest.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
    manifest.require_compatible()
    return manifest


def write_manifest(path: str | Path) -> CompatibilityManifest:
    """写出稳定排序的当前兼容性清单。

    Args:
        path: 输出 JSON 路径。

    Returns:
        已写出的 Manifest。

    """
    manifest = default_manifest()
    Path(path).write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = [
    "CURRENT_DATABASE_SCHEMA",
    "CompatibilityManifest",
    "default_manifest",
    "load_manifest",
    "write_manifest",
]
