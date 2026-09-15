"""通过 Universal SDK 引导并校验湾事通固定 Scope。"""

from __future__ import annotations

from rag_app.core.errors import RagError
from rag_app.core.models.management import KnowledgeBase, Project
from rag_app.sdk import RagSdk
from rag_app.wanshitong.errors import ScopeBindingError
from rag_app.wanshitong.mode import (
    WANSHITONG_KNOWLEDGE_BASE_NAME,
    WANSHITONG_PROJECT_NAME,
    WANSHITONG_SCOPE_KEY,
)
from rag_app.wanshitong.models import ScopeBinding, ScopeStatus
from rag_app.wanshitong.scope_store import ScopeBindingStore

_PROJECT_IDEMPOTENCY_KEY = f"{WANSHITONG_SCOPE_KEY}:project:v1"
_KNOWLEDGE_BASE_IDEMPOTENCY_KEY = (
    f"{WANSHITONG_SCOPE_KEY}:knowledge-base:v1"
)


class FixedScopeService:
    """确保唯一湾事通绑定存在且仍指向有效 Universal 对象。"""

    def __init__(self, sdk: RagSdk, store: ScopeBindingStore) -> None:
        """保存共享 SDK 与绑定 Store。

        Args:
            sdk: Product Runtime 已有的 Universal SDK。
            store: 复用同一控制数据库的绑定 Store。

        """
        self._sdk = sdk
        self._store = store

    def ensure(self) -> ScopeBinding:
        """复用既有绑定，或以持久幂等调用首次创建固定 Scope。

        Returns:
            已持久化且完成对象与父子关系校验的绑定。

        Raises:
            ScopeBindingError: 绑定损坏、对象无效或创建合同冲突。

        """
        binding = self._store.read()
        if binding is not None:
            self._validate(binding)
            return binding
        try:
            project = self._sdk.create_project(
                WANSHITONG_PROJECT_NAME,
                idempotency_key=_PROJECT_IDEMPOTENCY_KEY,
            )
            knowledge_base = self._sdk.create_knowledge_base(
                project.project_id,
                WANSHITONG_KNOWLEDGE_BASE_NAME,
                idempotency_key=_KNOWLEDGE_BASE_IDEMPOTENCY_KEY,
            )
        except RagError as error:
            raise ScopeBindingError(
                "湾事通固定 Scope 无法通过 Universal SDK 创建，启动已阻断。",
                stage="wanshitong.scope.create",
                details={"cause_code": error.code},
            ) from error
        binding = self._store.bind_unique(
            project.project_id, knowledge_base.knowledge_base_id
        )
        self._validate(binding)
        return binding

    def status(self) -> ScopeStatus:
        """重新校验绑定并返回不触发 Provider 的只读状态。

        Returns:
            当前固定 Project/KB 的安全摘要。

        Raises:
            ScopeBindingError: 绑定不存在或已不再有效。

        """
        binding = self._store.read()
        if binding is None:
            raise ScopeBindingError(
                "湾事通固定 Scope 尚未绑定。",
                stage="wanshitong.scope.status",
                details={"system_key": WANSHITONG_SCOPE_KEY},
            )
        project, knowledge_base = self._validate(binding)
        return ScopeStatus(
            project_id=project.project_id,
            project_name=project.name,
            knowledge_base_id=knowledge_base.knowledge_base_id,
            knowledge_base_name=knowledge_base.name,
            created_at=binding.created_at,
        )

    def _validate(
        self, binding: ScopeBinding
    ) -> tuple[Project, KnowledgeBase]:
        """确认绑定对象存在、可用且保持 Project/KB 父子关系。"""
        try:
            project = self._sdk.require_active_project(binding.project_id)
            knowledge_base = self._sdk.require_active_knowledge_base(
                binding.project_id, binding.knowledge_base_id
            )
        except RagError as error:
            raise ScopeBindingError(
                "湾事通固定 Scope 指向无效对象，启动已阻断。",
                stage="wanshitong.scope.validate",
                details={
                    "cause_code": error.code,
                    "system_key": WANSHITONG_SCOPE_KEY,
                },
            ) from error
        if knowledge_base.project_id != project.project_id:
            raise ScopeBindingError(
                "湾事通固定 Scope 的 Project/KB 父子关系无效，启动已阻断。",
                stage="wanshitong.scope.validate",
                details={"system_key": WANSHITONG_SCOPE_KEY},
            )
        return project, knowledge_base


__all__ = ["FixedScopeService"]
