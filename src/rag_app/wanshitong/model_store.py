"""在 Universal metadata 中保存湾事通模型引导的稳定绑定。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeVar

from pydantic import ValidationError

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.models.common import FrozenModel
from rag_app.wanshitong.errors import InternalModelConfigurationError
from rag_app.wanshitong.models import (
    InternalModelConnectionBinding,
    InternalModelProfileBinding,
    InternalModelReadyBinding,
)

InternalModelRole = Literal["embedding", "reranker", "llm"]
_BindingT = TypeVar("_BindingT", bound=FrozenModel)
_METADATA_NAMESPACE = "wanshitong.internal-models"
_PROFILE_KEY = "retrieval-profile"
_READY_KEY = "ready"


class InternalModelBindingStore:
    """以固定 metadata key 防止重跑时复制 Connection 或 Profile。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self._connections = connections

    def read_connection(
        self, role: InternalModelRole
    ) -> InternalModelConnectionBinding | None:
        """读取一个模型角色的 Connection 绑定。"""
        return self._read(role, InternalModelConnectionBinding)

    def bind_connection(
        self,
        role: InternalModelRole,
        connection_id: str,
        configuration_fingerprint: str,
    ) -> InternalModelConnectionBinding:
        """首次绑定角色，已有绑定必须与候选完全一致。"""
        candidate = InternalModelConnectionBinding(
            role=role,
            connection_id=connection_id,
            configuration_fingerprint=configuration_fingerprint,
            created_at=datetime.now(UTC).isoformat(),
        )
        return self._bind_unique(role, candidate)

    def read_profile(self) -> InternalModelProfileBinding | None:
        """读取固定 Retrieval Profile 绑定。"""
        return self._read(_PROFILE_KEY, InternalModelProfileBinding)

    def bind_profile(
        self,
        profile_revision_id: str,
        configuration_fingerprint: str,
    ) -> InternalModelProfileBinding:
        """首次绑定 Retrieval Profile，禁止静默改绑。"""
        candidate = InternalModelProfileBinding(
            profile_revision_id=profile_revision_id,
            configuration_fingerprint=configuration_fingerprint,
            created_at=datetime.now(UTC).isoformat(),
        )
        return self._bind_unique(_PROFILE_KEY, candidate)

    def read_ready(self) -> InternalModelReadyBinding | None:
        """读取完整配置锁标记。"""
        return self._read(_READY_KEY, InternalModelReadyBinding)

    def mark_ready(
        self, configuration_fingerprint: str
    ) -> InternalModelReadyBinding:
        """在全部模型设置完成后写入不可变锁标记。"""
        candidate = InternalModelReadyBinding(
            configuration_fingerprint=configuration_fingerprint,
            configured_at=datetime.now(UTC).isoformat(),
        )
        return self._bind_unique(_READY_KEY, candidate)

    def _read(self, key: str, model: type[_BindingT]) -> _BindingT | None:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE namespace=? AND key=?",
                (_METADATA_NAMESPACE, key),
            ).fetchone()
        if row is None:
            return None
        try:
            return model.model_validate_json(str(row["value"]))
        except ValidationError:
            raise InternalModelConfigurationError(
                "湾事通内网模型绑定已损坏，配置引导已阻断。",
                stage="wanshitong.models.read",
                details={"binding_key": key},
            ) from None

    def _bind_unique(self, key: str, candidate: _BindingT) -> _BindingT:
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE namespace=? AND key=?",
                (_METADATA_NAMESPACE, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO metadata(namespace, key, value) "
                    "VALUES (?, ?, ?)",
                    (
                        _METADATA_NAMESPACE,
                        key,
                        candidate.model_dump_json(),
                    ),
                )
                return candidate
        existing = self._read(key, type(candidate))
        if existing is None:
            raise AssertionError("湾事通模型绑定事务提交后不可缺失。")
        if existing.model_dump(exclude={"created_at", "configured_at"}) != (
            candidate.model_dump(exclude={"created_at", "configured_at"})
        ):
            raise InternalModelConfigurationError(
                "湾事通内网模型已绑定其他配置，禁止静默改绑。",
                stage="wanshitong.models.bind",
                details={"binding_key": key},
            )
        return existing


__all__ = ["InternalModelBindingStore", "InternalModelRole"]
