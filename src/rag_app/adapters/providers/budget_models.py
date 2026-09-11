"""账本和授权修订共用的不可变累计预算契约。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_BUSINESS_FIELDS = frozenset(
    {
        "scope_mode",
        "project_id",
        "knowledge_base_id",
        "approved_source_hashes",
        "approved_media_hashes",
        "allowed_models",
        "allowed_operations",
        "expires_at",
        "operation_request_limits",
    }
)
_BUSINESS_OPERATIONS = frozenset(
    {
        "embedding.document",
        "embedding.query",
        "reranking",
        "generation",
        "query.interpret",
        "query.rewrite",
        "image.ocr",
    }
)


@dataclass(frozen=True)
class BudgetCampaign:
    """不可由重启、续跑或重复创建改变的授权范围与累计上限。"""

    campaign_id: str
    authorization_id: str
    scope: str
    request_limit: int
    estimated_token_limit: int
    approved_payload_hashes: tuple[str, ...] = ()
    approved_text_hashes: tuple[str, ...] = ()
    approved_request_shape_hashes: tuple[str, ...] = ()
    approved_request_identities: tuple[str, ...] = ()
    provider_request_limits: Mapping[str, int] = field(default_factory=dict)
    provider_token_limits: Mapping[str, int] = field(default_factory=dict)
    step_request_limits: Mapping[str, int] = field(default_factory=dict)
    scope_mode: Literal["exact_payload", "knowledge_base"] = "exact_payload"
    project_id: str | None = None
    knowledge_base_id: str | None = None
    approved_source_hashes: tuple[str, ...] = ()
    approved_media_hashes: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    expires_at: str | None = None
    operation_request_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验可持久化的安全授权身份和上限。"""
        for value in (self.campaign_id, self.authorization_id, self.scope):
            if not _SAFE_IDENTIFIER.fullmatch(value):
                raise ValueError("预算身份必须使用安全标识符。")
        if any(
            type(value) is not int or value < 1
            for value in (self.request_limit, self.estimated_token_limit)
        ):
            raise ValueError("预算上限必须为正数。")
        for values in (
            self.approved_payload_hashes,
            self.approved_text_hashes,
            self.approved_request_shape_hashes,
            self.approved_request_identities,
            self.approved_source_hashes,
            self.approved_media_hashes,
        ):
            if any(not _HASH.fullmatch(value) for value in values):
                raise ValueError("预算批准集只保存 SHA256 身份。")
        for limits in (
            self.provider_request_limits,
            self.provider_token_limits,
            self.step_request_limits,
            self.operation_request_limits,
        ):
            if any(
                not _SAFE_IDENTIFIER.fullmatch(key)
                or type(value) is not int
                or value < 1
                for key, value in limits.items()
            ):
                raise ValueError("子预算必须包含安全身份和正整数上限。")
        self._validate_business_scope()

    def _validate_business_scope(self) -> None:
        if self.scope_mode == "exact_payload":
            if any(
                (
                    self.project_id,
                    self.knowledge_base_id,
                    self.approved_source_hashes,
                    self.approved_media_hashes,
                    self.allowed_models,
                    self.allowed_operations,
                    self.expires_at,
                    self.operation_request_limits,
                )
            ):
                raise ValueError("固定请求授权不能隐含知识库出网范围。")
            return
        if self.scope_mode != "knowledge_base":
            raise ValueError("预算范围模式无效。")
        if (
            not self.project_id
            or not _SAFE_IDENTIFIER.fullmatch(self.project_id)
            or not self.knowledge_base_id
            or not _SAFE_IDENTIFIER.fullmatch(self.knowledge_base_id)
            or not self.approved_source_hashes
            or not self.allowed_models
            or not self.allowed_operations
            or not self.approved_request_identities
            or not set(self.allowed_operations) <= _BUSINESS_OPERATIONS
            or set(self.operation_request_limits)
            != set(self.allowed_operations)
            or any(
                not _SAFE_IDENTIFIER.fullmatch(model)
                for model in self.allowed_models
            )
        ):
            raise ValueError(
                "知识库授权必须绑定来源、模型用途、请求身份和子限额。"
            )
        if not self.expires_at:
            raise ValueError("知识库授权必须具有明确到期时间。")
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            if expiry.tzinfo is None:
                raise ValueError("到期时间必须带时区。")
        except (TypeError, ValueError):
            raise ValueError("知识库授权到期时间无效。") from None


def campaign_configuration(campaign: BudgetCampaign) -> dict[str, object]:
    """序列化冻结配置，旧授权保持原 JSON 与批准集校验身份。

    Args:
        campaign: 已验证的固定请求或知识库范围。

    Returns:
        可保存到原账本 configuration 字段的配置。

    """
    values = asdict(campaign)
    if campaign.scope_mode == "exact_payload":
        for name in _BUSINESS_FIELDS:
            values.pop(name)
    return values
