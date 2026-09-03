"""绑定状态快照、默认 dry-run 且拒绝漂移的 P06 GC。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, TypeGuard

from rag_app.core.errors import ValidationFailed
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import GcPlan, RevisionVectorSpec
from rag_app.core.models.common import freeze_json_object
from rag_app.core.ports import BlobStorePort, VectorStorePort


class _GarbageCollectionControl(Protocol):
    """GarbageCollector 所需 SQLite 控制面。"""

    def gc_snapshot(
        self,
        *,
        protected_retired_count: int,
        grace_before: str,
    ) -> dict[str, object]:
        """读取 GC 权威快照。

        Args:
            protected_retired_count: 保留的最近 Retired 数量。
            grace_before: orphan 宽限截止时间。

        Returns:
            不含正文和路径的状态快照。

        """
        ...

    def save_gc_plan(
        self,
        plan_id: str,
        database_identity: str,
        snapshot: Mapping[str, object],
        plan_hash: str,
    ) -> None:
        """保存绑定数据库的 GC Plan。

        Args:
            plan_id: 稳定 Plan ID。
            database_identity: 不暴露路径的数据库身份。
            snapshot: 权威状态快照。
            plan_hash: canonical Plan hash。

        Returns:
            无返回值。

        """
        ...

    def load_gc_plan(self, plan_id: str) -> tuple[str, dict[str, object], str]:
        """读取待执行 Plan。

        Args:
            plan_id: 目标 Plan ID。

        Returns:
            数据库身份、快照和 hash。

        """
        ...

    def revision_vector_spec(self, revision_id: str) -> RevisionVectorSpec:
        """读取待删 Revision 的向量规格。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            完整向量规格。

        """
        ...

    def delete_retired_revision(self, revision_id: str) -> None:
        """删除已确认的 Retired 控制记录。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            无返回值。

        """
        ...

    def delete_gc_revision(self, revision_id: str) -> None:
        """删除已逐项重验的 Revision 控制记录。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            无返回值。

        """
        ...

    def gc_revision_exists(self, revision_id: str) -> bool:
        """检查 Revision 控制行是否存在。

        Args:
            revision_id: 目标 Revision ID。

        Returns:
            控制行仍存在时返回 True。

        """
        ...

    def gc_plan_items(self, plan_id: str) -> tuple[dict[str, object], ...]:
        """读取 Plan 的逐项进度。

        Args:
            plan_id: 持久 GC Plan ID。

        Returns:
            按稳定顺序排列的 item 状态。

        """
        ...

    def claim_gc_plan_item(
        self,
        plan_id: str,
        item_type: str,
        item_id: str,
        expected_snapshot_hash: str,
    ) -> str:
        """原子领取一个耐久 GC item。

        Args:
            plan_id: 持久 GC Plan ID。
            item_type: `revision` 或 `blob`。
            item_id: 待处理对象的稳定 ID。
            expected_snapshot_hash: 计划时绑定的快照摘要。

        Returns:
            领取后的持久状态。

        """
        ...

    def set_gc_plan_item_state(
        self,
        plan_id: str,
        item_type: str,
        item_id: str,
        state: str,
        *,
        safe_error: str | None = None,
    ) -> None:
        """记录 GC item 外部副作用进度。

        Args:
            plan_id: 持久 GC Plan ID。
            item_type: `revision` 或 `blob`。
            item_id: 正在处理的对象 ID。
            state: 外部副作用完成后的状态。
            safe_error: 可持久化的安全错误类别。

        Returns:
            无返回值。

        """
        ...

    def reconcile_blob_inventory(
        self,
        physical: Mapping[str, str],
    ) -> dict[str, tuple[str, ...]]:
        """持久化物理 CAS 与 catalog 差集。

        Args:
            physical: Artifact ID 到内容摘要的受控物理清单。

        Returns:
            physical-only、catalog-only 与一致对象的 ID 集合。

        """
        ...

    def claim_orphan_blob(self, artifact_id: str) -> bool:
        """原子领取无引用 Artifact。

        Args:
            artifact_id: 目标 Artifact ID。

        Returns:
            成功领取时为 True。

        """
        ...

    def finish_orphan_blob(self, artifact_id: str, *, deleted: bool) -> None:
        """完成或回滚 Blob 删除状态。

        Args:
            artifact_id: 已领取的 Artifact ID。
            deleted: 物理删除是否成功。

        Returns:
            无返回值。

        """
        ...

    def mark_gc_plan_applied(self, plan_id: str) -> None:
        """标记完整执行成功的 Plan。

        Args:
            plan_id: 目标 Plan ID。

        Returns:
            无返回值。

        """
        ...


class GarbageCollector:
    """先 plan，apply 前重算，按 Vector、Blob、SQLite 顺序清理。"""

    def __init__(
        self,
        control: _GarbageCollectionControl,
        vector_store: VectorStorePort,
        blob_store: BlobStorePort,
        *,
        database_identity: str,
    ) -> None:
        """注入控制面、物理 Stores 与数据库身份。

        Args:
            control: SQLite GC 控制面。
            vector_store: revision collection Store。
            blob_store: content-addressed Blob Store。
            database_identity: 不暴露路径的数据库身份摘要。

        Returns:
            无返回值。

        """
        self._control = control
        self._vector_store = vector_store
        self._blob_store = blob_store
        self._database_identity = database_identity

    def plan(
        self,
        *,
        protected_retired_count: int,
        grace_before: str,
    ) -> GcPlan:
        """创建但不执行删除的 GC Plan。

        Args:
            protected_retired_count: 每个 KB 保留最近 retired 数量。
            grace_before: orphan 必须早于的 ISO 时间。

        Returns:
            已持久化的 dry-run Plan。

        """
        if protected_retired_count < 0:
            raise ValueError("protected retired count 不能为负。")
        snapshot = self._control.gc_snapshot(
            protected_retired_count=protected_retired_count,
            grace_before=grace_before,
        )
        payload = {
            "database_identity": self._database_identity,
            "snapshot": snapshot,
        }
        plan_hash = canonical_sha256(payload)
        plan_id = deterministic_id("gcplan", plan_hash)
        plan = GcPlan(
            plan_id=plan_id,
            database_identity=self._database_identity,
            snapshot=freeze_json_object(snapshot),
            plan_hash=plan_hash,
        )
        self._control.save_gc_plan(
            plan_id,
            self._database_identity,
            snapshot,
            plan_hash,
        )
        return plan

    def apply(self, plan_id: str) -> None:
        """重算无漂移后执行指定 Plan。

        Args:
            plan_id: 用户显式指定的已持久化 Plan ID。

        Returns:
            无返回值。

        Raises:
            ValidationFailed: 数据库身份、状态快照或 plan hash 漂移。

        """
        database_identity, snapshot, plan_hash = self._control.load_gc_plan(
            plan_id
        )
        if database_identity != self._database_identity:
            raise ValidationFailed(
                "GC Plan 数据库身份不匹配。", stage="gc.apply"
            )
        expected_hash = canonical_sha256(
            {"database_identity": database_identity, "snapshot": snapshot}
        )
        if expected_hash != plan_hash:
            raise ValidationFailed("GC Plan hash 不匹配。", stage="gc.apply")
        protected_retired_count = snapshot.get("protected_retired_count")
        grace_before = snapshot.get("grace_before")
        revision_candidates = snapshot.get("revision_candidates")
        orphan_blob_candidates = snapshot.get("orphan_blob_candidates")
        if (
            not isinstance(protected_retired_count, int)
            or not isinstance(grace_before, str)
            or not _is_identifier_sequence(revision_candidates)
            or not _is_identifier_sequence(orphan_blob_candidates)
        ):
            raise ValidationFailed(
                "GC Plan 状态快照结构损坏。", stage="gc.apply"
            )
        items = self._control.gc_plan_items(plan_id)
        current = self._control.gc_snapshot(
            protected_retired_count=protected_retired_count,
            grace_before=grace_before,
        )
        progressed = any(item.get("state") != "planned" for item in items)
        if current != snapshot and not progressed:
            raise ValidationFailed("GC Plan 状态快照已漂移。", stage="gc.apply")
        by_identity = {
            (str(item["item_type"]), str(item["item_id"])): item
            for item in items
        }
        for revision_id in revision_candidates:
            item = by_identity.get(("revision", str(revision_id)))
            if item is None:
                raise ValidationFailed(
                    "GC Revision item 缺失。", stage="gc.apply"
                )
            self._apply_revision_item(plan_id, str(revision_id), item)
        for artifact_id in orphan_blob_candidates:
            item = by_identity.get(("blob", str(artifact_id)))
            if item is None:
                raise ValidationFailed("GC Blob item 缺失。", stage="gc.apply")
            self._apply_blob_item(plan_id, str(artifact_id), item)
        self._control.mark_gc_plan_applied(plan_id)

    def reconcile_filesystem(self) -> dict[str, tuple[str, ...]]:
        """盘点物理 CAS 并记录 physical/catalog 差集。

        Args:
            无参数；读取注入的受控 Blob Store。

        Returns:
            三类安全 Blob ID 集合。

        """
        physical = {
            item.blob_id: item.content_sha256
            for item in self._blob_store.audit_inventory()
        }
        return self._control.reconcile_blob_inventory(physical)

    def _apply_revision_item(
        self,
        plan_id: str,
        revision_id: str,
        item: Mapping[str, object],
    ) -> None:
        expected_hash = str(item["expected_snapshot_hash"])
        state = self._control.claim_gc_plan_item(
            plan_id, "revision", revision_id, expected_hash
        )
        if state == "completed":
            return
        try:
            if state == "claimed":
                if not self._control.gc_revision_exists(revision_id):
                    self._control.set_gc_plan_item_state(
                        plan_id,
                        "revision",
                        revision_id,
                        "sqlite_deleted",
                    )
                    state = "sqlite_deleted"
                else:
                    spec = self._control.revision_vector_spec(revision_id)
                    if self._vector_store.revision_exists(spec):
                        self._vector_store.delete_revision(spec)
                    self._control.set_gc_plan_item_state(
                        plan_id, "revision", revision_id, "vector_deleted"
                    )
                    state = "vector_deleted"
            if state == "vector_deleted":
                if self._control.gc_revision_exists(revision_id):
                    self._control.delete_gc_revision(revision_id)
                self._control.set_gc_plan_item_state(
                    plan_id, "revision", revision_id, "sqlite_deleted"
                )
                state = "sqlite_deleted"
            if state == "sqlite_deleted":
                self._control.set_gc_plan_item_state(
                    plan_id, "revision", revision_id, "completed"
                )
        except Exception as error:
            self._control.set_gc_plan_item_state(
                plan_id,
                "revision",
                revision_id,
                "failed_retryable",
                safe_error=type(error).__name__,
            )
            raise

    def _apply_blob_item(
        self,
        plan_id: str,
        artifact_id: str,
        item: Mapping[str, object],
    ) -> None:
        expected_hash = str(item["expected_snapshot_hash"])
        state = self._control.claim_gc_plan_item(
            plan_id, "blob", artifact_id, expected_hash
        )
        if state == "completed":
            return
        try:
            if state == "claimed":
                if not self._control.claim_orphan_blob(artifact_id):
                    raise ValidationFailed(
                        "GC Blob 引用状态已漂移。", stage="gc.apply"
                    )
                self._blob_store.delete(artifact_id)
                self._control.finish_orphan_blob(artifact_id, deleted=True)
                self._control.set_gc_plan_item_state(
                    plan_id, "blob", artifact_id, "blob_reconciled"
                )
                state = "blob_reconciled"
            if state == "blob_reconciled":
                self._control.set_gc_plan_item_state(
                    plan_id, "blob", artifact_id, "completed"
                )
        except Exception as error:
            self._control.finish_orphan_blob(artifact_id, deleted=False)
            self._control.set_gc_plan_item_state(
                plan_id,
                "blob",
                artifact_id,
                "failed_retryable",
                safe_error=type(error).__name__,
            )
            raise


def _is_identifier_sequence(value: object) -> TypeGuard[Sequence[str]]:
    """判断 JSON 值是否为字符串标识符序列。"""
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and all(isinstance(item, str) for item in value)
    )


__all__ = ["GarbageCollector"]
