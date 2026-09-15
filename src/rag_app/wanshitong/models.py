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
    ready: Literal[True] = True
    system_key: Literal["wanshitong-default-scope"] = WANSHITONG_SCOPE_KEY
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    project_name: str = Field(min_length=1, max_length=200)
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    knowledge_base_name: str = Field(min_length=1, max_length=200)
    created_at: str = Field(min_length=1)


class InternalModelConnectionBinding(FrozenModel):
    """把一个湾事通模型角色固定到既有 Provider Connection。"""

    schema_version: Literal[1] = 1
    role: Literal["embedding", "reranker", "llm"]
    connection_id: str = Field(pattern=r"^conn_[0-9a-f]{32}$")
    configuration_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: str = Field(min_length=1)


class InternalModelProfileBinding(FrozenModel):
    """把固定知识库绑定到一个 Universal Retrieval Profile。"""

    schema_version: Literal[1] = 1
    profile_revision_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    configuration_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: str = Field(min_length=1)


class InternalModelReadyBinding(FrozenModel):
    """标记内网模型、检索方案与生成设置已完整冻结。"""

    schema_version: Literal[1] = 1
    configuration_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configured_at: str = Field(min_length=1)


class InternalModelConfigurationReport(FrozenModel):
    """CLI 可输出且不包含端点和 Secret 的配置摘要。"""

    mode: Literal["wanshitong"] = "wanshitong"
    status: Literal["configured"] = "configured"
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    embedding_connection_id: str = Field(pattern=r"^conn_[0-9a-f]{32}$")
    reranker_connection_id: str = Field(pattern=r"^conn_[0-9a-f]{32}$")
    llm_connection_id: str = Field(pattern=r"^conn_[0-9a-f]{32}$")
    retrieval_profile_revision_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    embedding_model: str = Field(min_length=1, max_length=200)
    embedding_dimension: int = Field(gt=0, le=65536)
    reranker_model: str = Field(min_length=1, max_length=200)
    llm_model: str = Field(min_length=1, max_length=200)
    ocr_api_path: Literal["/v1/ocr"] = "/v1/ocr"
    ocr_configured: bool
    pdf_layout_parsing_configured: bool


__all__ = [
    "InternalModelConfigurationReport",
    "InternalModelConnectionBinding",
    "InternalModelProfileBinding",
    "InternalModelReadyBinding",
    "ScopeBinding",
    "ScopeStatus",
]
