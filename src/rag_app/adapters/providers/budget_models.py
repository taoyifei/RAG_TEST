"""账本和授权修订共用的不可变累计预算契约。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


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
        ):
            if any(not _HASH.fullmatch(value) for value in values):
                raise ValueError("预算批准集只保存 SHA256 身份。")
        for limits in (
            self.provider_request_limits,
            self.provider_token_limits,
            self.step_request_limits,
        ):
            if any(
                not _SAFE_IDENTIFIER.fullmatch(key)
                or type(value) is not int
                or value < 1
                for key, value in limits.items()
            ):
                raise ValueError("子预算必须包含安全身份和正整数上限。")
