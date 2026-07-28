"""加载并应用唯一的 DOCX 语料元数据策略。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from rag_app.contracts import DocumentMetadata
from rag_app.strict_json import load_json_file

__all__ = ["CorpusPolicy", "CorpusPolicyOverride"]


class CorpusPolicyOverride(DocumentMetadata):
    """一个 DOCX 相对路径的完整元数据覆盖。"""

    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _validate_relative_path(value)


class CorpusPolicy(BaseModel):
    """严格、只读且可规范化摘要的语料策略。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^1$")
    defaults: DocumentMetadata
    overrides: tuple[CorpusPolicyOverride, ...] = ()

    @model_validator(mode="after")
    def _validate_policy(self) -> Self:
        if (
            self.defaults.document_status != "active"
            or self.defaults.authority_level != "official"
        ):
            raise ValueError("语料默认值必须是 active/official。")
        paths = tuple(item.path for item in self.overrides)
        if len(set(paths)) != len(paths):
            raise ValueError("语料覆盖路径不能重复。")
        folded = tuple(path.casefold() for path in paths)
        if len(set(folded)) != len(folded):
            raise ValueError("语料覆盖路径存在大小写折叠冲突。")
        return self

    @classmethod
    def load(cls, path: Path) -> CorpusPolicy:
        """从 UTF-8 JSON 文件加载严格语料策略。

        Args:
            path: 只读 corpus-policy.json 路径。

        Returns:
            已通过 schema 和语义校验的策略。

        Raises:
            ValueError: JSON 重复字段、格式或策略语义无效。

        """
        return cls.model_validate(
            load_json_file(path, label="corpus policy")
        )

    def semantic_sha256(self) -> str:
        """计算与 JSON 排版和覆盖顺序无关的语义摘要。

        Args:
            无参数。

        Returns:
            64 位小写十六进制 SHA256。

        """
        payload = {
            "schema_version": self.schema_version,
            "defaults": _metadata_payload(self.defaults),
            "overrides": [
                {
                    "path": override.path,
                    **_metadata_payload(override),
                }
                for override in sorted(
                    self.overrides,
                    key=lambda item: item.path,
                )
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def resolve(
        self,
        *,
        input_root: Path,
        discovered_paths: tuple[str, ...],
    ) -> dict[str, DocumentMetadata]:
        """为本次目录快照生成完整文档元数据。

        Args:
            input_root: 受控 DOCX 根目录。
            discovered_paths: 本次发现的全部相对 POSIX 路径。

        Returns:
            以每个发现路径为键的完整元数据。

        Raises:
            ValueError: 路径重复、覆盖多余、符号链接或解析后越界。

        """
        root = input_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("语料根路径必须是目录。")
        normalized = tuple(
            _validate_relative_path(path) for path in discovered_paths
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("本次发现的 DOCX 路径重复。")
        folded = tuple(path.casefold() for path in normalized)
        if len(set(folded)) != len(folded):
            raise ValueError("本次 DOCX 路径存在大小写折叠冲突。")
        discovered = set(normalized)
        override_by_path = {
            override.path: override for override in self.overrides
        }
        unknown = sorted(set(override_by_path) - discovered)
        if unknown:
            raise ValueError(
                f"语料覆盖指向本次未发现的 DOCX：{','.join(unknown)}"
            )
        resolved: dict[str, DocumentMetadata] = {}
        for relative_path in normalized:
            _require_safe_candidate(root, relative_path)
            override = override_by_path.get(relative_path)
            resolved[relative_path] = (
                self.defaults
                if override is None
                else DocumentMetadata(
                    document_status=override.document_status,
                    authority_level=override.authority_level,
                    effective_from=cast(
                        datetime | None,
                        _canonical_datetime(override.effective_from),
                    ),
                    effective_to=cast(
                        datetime | None,
                        _canonical_datetime(override.effective_to),
                    ),
                )
            )
        return resolved


def _validate_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("语料覆盖路径必须使用 POSIX 斜杠。")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or path.suffix.casefold() != ".docx"
    ):
        raise ValueError("语料覆盖路径必须是规范 DOCX 相对 POSIX 路径。")
    return value


def _require_safe_candidate(root: Path, relative_path: str) -> None:
    candidate = root
    for part in PurePosixPath(relative_path).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError("语料路径不能经过符号链接。")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("语料路径解析后越界或不是文件。")


def _metadata_payload(metadata: DocumentMetadata) -> dict[str, str | None]:
    return {
        "document_status": metadata.document_status,
        "authority_level": metadata.authority_level,
        "effective_from": _canonical_datetime(metadata.effective_from),
        "effective_to": _canonical_datetime(metadata.effective_to),
    }


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
