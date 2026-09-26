"""单一公共问答应用的发布契约与原生配置核对。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from wanshitong_gateway.weknora.admin import NativeAdminClient
from wanshitong_gateway.weknora.client import NativeHttpError, WeKnoraClient

APPLICATION_ID = "wanshitong-public"
APPLICATION_NAME = "湾事通用户问答"
_MAX_KB_ID_LENGTH = 128


class PageSettings(BaseModel):
    """只允许公开页面展示和已有网关能力的有限开关。"""

    model_config = ConfigDict(extra="forbid")

    welcome_text: str = Field(default="", max_length=500)
    input_placeholder: str = Field(default="", max_length=200)
    show_recommendations: bool = True
    show_history: bool = True
    allow_feedback: bool = True
    allow_source_download: bool = True


class AnswerSettings(BaseModel):
    """显式复制现行回答参数，避免原生 EnsureDefaults 悄悄改值。"""

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=128)
    rerank_model_id: str = Field(max_length=128)
    system_prompt_id: str = Field(min_length=1, max_length=128)
    system_prompt: str
    context_template_id: str = Field(min_length=1, max_length=128)
    context_template: str
    temperature: float = Field(ge=0, le=1)
    max_completion_tokens: int = Field(ge=0)
    thinking: bool
    citation_enabled: bool
    multi_turn_enabled: bool
    history_turns: int = Field(ge=1)
    embedding_top_k: int = Field(ge=1)
    keyword_threshold: float = Field(gt=0, le=1)
    vector_threshold: float = Field(gt=0, le=1)
    rerank_top_k: int = Field(ge=1)
    rerank_threshold: float = Field(ge=0, le=1)
    enable_rewrite: bool
    enable_query_expansion: bool
    rewrite_prompt_system: str
    rewrite_prompt_user: str
    fallback_strategy: Literal["fixed", "model"]
    fallback_response: str
    fallback_prompt: str

    @model_validator(mode="after")
    def require_prompt_snapshot(self) -> Self:
        """保存已核实的原生模板正文，避免隐式继承以后发生漂移。"""
        if not self.system_prompt.strip() or not self.context_template.strip():
            raise ValueError("system and context prompts must be verified")
        if self.enable_rewrite and (
            not self.rewrite_prompt_system.strip()
            or not self.rewrite_prompt_user.strip()
        ):
            raise ValueError("rewrite prompts must be verified")
        return self

    def native_config(self, kb_ids: tuple[str, ...]) -> dict[str, Any]:
        """形成可回读的原生 quick-answer 配置，固定关闭旁路能力。"""
        return {
            **self.model_dump(),
            "agent_mode": "quick-answer",
            "kb_selection_mode": "selected",
            "knowledge_bases": list(kb_ids),
            "retrieve_kb_only_when_mentioned": False,
            "web_search_enabled": False,
            "web_fetch_enabled": False,
            "mcp_selection_mode": "none",
            "mcp_services": [],
            "skills_selection_mode": "none",
            "selected_skills": [],
            "sandbox_config_id": "",
            "memory_enabled": False,
        }


class SavePublicAppRequest(BaseModel):
    """全量保存；首次绑定须由管理员明确确认运行值已核实。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    migration_confirmed: bool = False
    knowledge_base_ids: list[str] = Field(min_length=1)
    answer_settings: AnswerSettings
    page_settings: PageSettings

    def kb_ids(self) -> tuple[str, ...]:
        """不接受空值、路径片段或重复 ID。"""
        values = tuple(item.strip() for item in self.knowledge_base_ids)
        if (
            any(
                not item
                or len(item) > _MAX_KB_ID_LENGTH
                or "/" in item
                or "\\" in item
                for item in values
            )
            or len(values) != len(set(values))
        ):
            raise ValueError("invalid knowledge base scope")
        return values


def config_digest(config: dict[str, Any]) -> str:
    """对原生保存后的完整配置建立非秘密内容摘要。"""
    encoded = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def answer_from_config(config: dict[str, Any]) -> AnswerSettings:
    """原生回读必须含所有受控字段，不用本地默认掩盖缺项。"""
    try:
        fields = {
            key: config[key]
            for key in AnswerSettings.model_fields
            if key in config
        }
        return AnswerSettings.model_validate(fields)
    except ValidationError as error:
        raise NativeHttpError(503, "原生应用缺少回答设置。") from error


def uses_public_scope(
    config: dict[str, Any], kb_ids: tuple[str, ...]
) -> bool:
    """原生回读必须保持 KB 范围和关闭的外部执行能力。"""
    return (
        config.get("agent_mode") == "quick-answer"
        and config.get("kb_selection_mode") == "selected"
        and config.get("knowledge_bases") == list(kb_ids)
        and config.get("retrieve_kb_only_when_mentioned") is False
        and config.get("web_search_enabled") is False
        and config.get("web_fetch_enabled") is False
        and config.get("mcp_selection_mode") == "none"
        and config.get("skills_selection_mode") == "none"
        and config.get("sandbox_config_id", "") == ""
        and config.get("memory_enabled") is False
    )


async def validate_binding(
    native: WeKnoraClient,
    admin_native: NativeAdminClient,
    binding: dict[str, Any],
) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    """每次新请求核对公共 Key、原生 Agent 与已发布范围。"""
    agent_id = binding["native_agent_id"]
    kb_ids = binding["knowledge_base_ids"]
    if (
        not isinstance(agent_id, str)
        or not isinstance(kb_ids, tuple)
        or not kb_ids
    ):
        raise NativeHttpError(503, "公共应用绑定无效。")
    agent = await native.public_agent(agent_id)
    config = agent.get("config")
    if not isinstance(config, dict):
        raise NativeHttpError(503, "公共应用原生配置不可用。")
    if (
        config_digest(config) != binding["native_config_digest"]
        or not uses_public_scope(config, kb_ids)
        or not config.get("model_id")
    ):
        raise NativeHttpError(503, "公共应用配置已漂移。")
    key_scope = await admin_native.public_key_scope(
        native.api_key_fingerprint()
    )
    if not set(kb_ids).issubset(key_scope.knowledge_base_ids):
        raise NativeHttpError(503, "公共 Key 无权读取已发布资料。")
    return agent_id, kb_ids, config
