"""索引 revision 的身份和生命周期模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from rag_app.core.models.common import FrozenModel


class IndexRevisionState(StrEnum):
    """索引构建与激活状态。"""

    BUILDING = "building"
    BUILDING_DEGRADED = "building_degraded"
    READY = "ready"
    ACTIVE = "active"
    RETIRED = "retired"


class IndexRevisionRef(FrozenModel):
    """绑定知识库和 index fingerprint 的 revision 引用。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: IndexRevisionState
