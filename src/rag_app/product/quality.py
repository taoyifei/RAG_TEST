"""持久质量记录与配置绑定；连接成功不能替代独立质量验证。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from rag_app.core.identifiers import canonical_json, canonical_sha256
from rag_app.core.models.common import FrozenModel
from rag_app.product.verification import endpoint_identity, profile_specs

if TYPE_CHECKING:
    from rag_app.adapters.stores.sqlite_connection import (
        SqliteConnectionFactory,
    )
    from rag_app.product.control_store import ProductControlStore

QualityKind = Literal[
    "local_contract_verified",
    "offline_evaluation_ready",
    "provider_connectivity_verified",
    "dual_slot_function_verified",
    "retrieval_quality_verified",
    "release_candidate_verified",
]
_MIN_LABELED_QUERIES = 20
_MIN_NEGATIVE_QUERIES = 10
_MIN_SOURCE_PRECISION = 0.9
_MIN_RECALL = 0.8


class QualityValidationRecord(FrozenModel):
    """只由受信任验收入口导入，普通 Profile 表单不可写入。"""

    profile_revision_id: str
    kind: QualityKind
    validation_mode: Literal["offline", "mock", "live"]
    run_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_fingerprint: str
    serving_fingerprint: str
    gates: dict[str, bool]
    independent_holdout: bool = False
    labeled_queries: int = Field(default=0, ge=0)
    negative_queries: int = Field(default=0, ge=0)
    citation_source_precision: float = Field(default=0, ge=0, le=1)
    recall: float = Field(default=0, ge=0, le=1)
    negative_leakage: float = Field(default=1, ge=0, le=1)


class ProductQualityStore:
    """保存不可变验收证据并按当前解析合同判断适用性。"""

    def __init__(
        self, connections: SqliteConnectionFactory, control: ProductControlStore
    ) -> None:
        self._connections = connections
        self._control = control

    def binding_identity(self, profile_id: str) -> str:
        """绑定方案、连接版本与授权端点，不读取 Key 字节。

        Args:
            profile_id: 精确 Profile Revision。

        Returns:
            当前验证与缓存的安全失效身份。

        """
        profile = self._control.get_profile(profile_id)
        ids = {
            profile.primary_connection_id,
            profile.standby_connection_id,
            profile.reranker_connection_id,
        } - {None}
        bindings = []
        for connection_id in sorted(item for item in ids if item is not None):
            connection = self._control.get_connection(connection_id)
            bindings.append(
                (
                    connection_id,
                    connection.configuration_version,
                    self._control.credential_version(connection.credential_id),
                    endpoint_identity(connection),
                    connection.enabled,
                )
            )
        return canonical_sha256(
            {
                "index": profile.index_semantic_fingerprint,
                "serving": profile.serving_fingerprint,
                "specs": [
                    spec.semantic_identity()
                    for spec in profile_specs(
                        profile, self._control.get_connection
                    )
                ],
                "connections": bindings,
            }
        )

    def record(self, record: QualityValidationRecord) -> str:
        """接受已有验收入口的脱敏报告，按固定门槛计算接受状态。

        Args:
            record: 带数据集、报告摘要和独立标签计数的结果。

        Returns:
            新增的不可变记录 ID。

        Raises:
            ValueError: 报告与当前方案指纹不匹配。

        """
        profile = self._control.get_profile(record.profile_revision_id)
        if (record.index_fingerprint, record.serving_fingerprint) != (
            profile.index_semantic_fingerprint,
            profile.serving_fingerprint,
        ):
            raise ValueError("质量报告的方案指纹不匹配。")
        required = {
            "local_contract_verified": {"check", "smoke", "frontend"},
            "offline_evaluation_ready": {"offline_eval"},
            "provider_connectivity_verified": {"required_operations"},
            "dual_slot_function_verified": {
                "primary",
                "standby",
                "failover",
                "isolation",
            },
            "retrieval_quality_verified": {
                "independent_labels",
                "source_precision",
                "recall",
                "negative_leakage",
            },
            "release_candidate_verified": {
                "release_verify",
                "release_acceptance",
            },
        }[record.kind]
        accepted = required <= record.gates.keys() and all(
            record.gates.values()
        )
        if record.kind == "retrieval_quality_verified":
            accepted = accepted and (
                record.validation_mode == "live"
                and record.independent_holdout
                and record.labeled_queries >= _MIN_LABELED_QUERIES
                and record.negative_queries >= _MIN_NEGATIVE_QUERIES
                and record.citation_source_precision >= _MIN_SOURCE_PRECISION
                and record.recall >= _MIN_RECALL
                and record.negative_leakage == 0
            )
        record_id = f"qval_{secrets.token_hex(16)}"
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO quality_validation_records VALUES (?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    record_id,
                    record.profile_revision_id,
                    record.kind,
                    record.validation_mode,
                    int(accepted),
                    self.binding_identity(record.profile_revision_id),
                    canonical_json(record.model_dump(mode="json")),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return record_id

    def states(self, profile_id: str) -> dict[str, str]:
        """仅返回与当前连接及方案一致的已接受证据。

        Args:
            profile_id: 当前方案。

        Returns:
            证据种类到验证模式；缺失键表示未验证。

        """
        try:
            binding = self.binding_identity(profile_id)
        except ValueError:
            # 历史策略需显式迁移，但不得阻止本地管理页读取未验证状态。
            return {}
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT kind, validation_mode FROM quality_validation_records "
                "WHERE profile_revision_id=? AND binding_identity=? AND "
                "accepted=1 ORDER BY created_at",
                (profile_id, binding),
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def calibrated_spaces(self, profile_id: str) -> tuple[str, ...]:
        """从独立 Live 质量记录取得当前方案允许的真实向量空间。

        Args:
            profile_id: 精确方案。

        Returns:
            未验证时为空；与当前模型和参数绑定的空间序列。

        """
        if self.states(profile_id).get("retrieval_quality_verified") != "live":
            return ()
        profile = self._control.get_profile(profile_id)
        return tuple(
            ":".join(
                (
                    role,
                    spec.provider_id,
                    spec.model,
                    str(spec.dimension),
                    spec.normalization,
                    spec.adapter_revision,
                )
            )
            for role, spec in zip(
                ("primary", "standby"),
                profile_specs(profile, self._control.get_connection),
                strict=False,
            )
        )
