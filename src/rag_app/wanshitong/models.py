"""湾事通固定 Scope 的持久绑定与只读状态模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rag_app.core.models.common import FrozenModel
from rag_app.wanshitong.mode import WANSHITONG_SCOPE_KEY


class ScopeBinding(FrozenModel):
    """以服务端生成 ID 固定一个 Project/KB 父子作用域。"""

    schema_version: Literal[1] = 1
    system_key: Literal["wanshitong-default-scope"] = WANSHITONG_SCOPE_KEY
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    created_at: str = Field(min_length=1)


class ScopeStatus(FrozenModel):
    """管理员可读取的固定 Scope 当前状态。"""

    mode: Literal["wanshitong"] = "wanshitong"
    ready: bool = True
    system_key: Literal["wanshitong-default-scope"] = WANSHITONG_SCOPE_KEY
    project_id: str | None = Field(
        default=None, pattern=r"^prj_[0-9a-f]{32}$"
    )
    project_name: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    knowledge_base_id: str | None = Field(
        default=None, pattern=r"^kb_[0-9a-f]{32}$"
    )
    knowledge_base_name: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    created_at: str | None = Field(default=None, min_length=1)
    blocker_code: str | None = None
    blocker_message: str | None = None


__all__ = ["ScopeBinding", "ScopeStatus"]
