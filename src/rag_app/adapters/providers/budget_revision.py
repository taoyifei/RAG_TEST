"""持久预算的追加审批定义；计划与普通配置本身不能授予额度。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime

from rag_app.adapters.providers.budget_errors import BudgetBlockedError
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.core.identifiers import canonical_sha256

_MAX_TEXT = 500
_SPACE_CODEPOINT = 32
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


@dataclass(frozen=True)
class BudgetAuthorizationRevision:
    """实际管理员确认的累计总上限，保持同一 campaign 与批准数据集。"""

    revision_id: str
    campaign_id: str
    previous_revision_id: str | None
    authorization_id: str
    approval_reference: str
    approver: str
    approved_at: str
    scope: str
    payload_set_identity: str
    request_limit: int
    estimated_token_limit: int
    reason: str
    status: str = "PROPOSED"
    expires_at: str | None = None
    provider_request_limits: dict[str, int] = field(default_factory=dict)
    provider_token_limits: dict[str, int] = field(default_factory=dict)
    step_request_limits: dict[str, int] = field(default_factory=dict)

    def validate(self, *, now: datetime | None = None) -> None:
        """拒绝缺批准、空责任人、过期和不安全审批记录。

        Args:
            now: 可注入的当前 UTC 时间；默认系统时间。

        Returns:
            无返回值；错误不会被转成有效审批。

        """
        identifiers: tuple[str, ...] = (
            self.revision_id,
            self.campaign_id,
            self.authorization_id,
            self.approval_reference,
            self.scope,
        )
        if self.previous_revision_id is not None:
            identifiers += (self.previous_revision_id,)
        if any(
            not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value)
            for value in identifiers
        ):
            raise BudgetBlockedError("BUDGET_REVISION_ID_INVALID")
        if self.status != "APPROVED":
            raise BudgetBlockedError("BUDGET_REVISION_NOT_APPROVED")
        for value in (self.approver, self.reason):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > _MAX_TEXT
                or any(ord(char) < _SPACE_CODEPOINT for char in value)
            ):
                raise BudgetBlockedError("BUDGET_APPROVAL_METADATA_REQUIRED")
        current = now or datetime.now(UTC)
        try:
            approved = datetime.fromisoformat(self.approved_at)
            expiry = (
                None
                if self.expires_at is None
                else datetime.fromisoformat(self.expires_at)
            )
            if approved.tzinfo is None or approved > current:
                raise ValueError("审批时间无效。")
            if expiry is not None and (
                expiry.tzinfo is None or expiry <= current or expiry <= approved
            ):
                raise ValueError("审批已经过期。")
        except (TypeError, ValueError):
            raise BudgetBlockedError("BUDGET_REVISION_TIME_INVALID") from None

    def effective_campaign(self, base: BudgetCampaign) -> BudgetCampaign:
        """保留原授权和批准文本，只替换已人工批准的累计上限。

        Args:
            base: 不可变的初始授权。

        Returns:
            适用于所有历史与未来 attempt 的累计配置。

        """
        if (
            self.campaign_id != base.campaign_id
            or self.authorization_id != base.authorization_id
            or self.scope != base.scope
            or self.payload_set_identity != budget_payload_set_identity(base)
        ):
            raise BudgetBlockedError("BUDGET_REVISION_SCOPE_MISMATCH")
        if any(
            not set(original) <= set(updated)
            for original, updated in (
                (base.provider_token_limits, self.provider_token_limits),
                (base.provider_request_limits, self.provider_request_limits),
                (base.step_request_limits, self.step_request_limits),
            )
        ):
            raise BudgetBlockedError("BUDGET_REVISION_SUBLIMITS_REQUIRED")
        return replace(
            base,
            request_limit=self.request_limit,
            estimated_token_limit=self.estimated_token_limit,
            provider_request_limits=self.provider_request_limits,
            provider_token_limits=self.provider_token_limits,
            step_request_limits=self.step_request_limits,
        )


def budget_payload_set_identity(campaign: BudgetCampaign) -> str:
    """计算审批必须准确绑定的公开请求批准集身份。

    Args:
        campaign: 已绑定的原始预算授权。

    Returns:
        包含文本、请求形状和端点请求身份的 SHA256。

    """
    return canonical_sha256(
        {
            key: sorted(value)
            for key, value in asdict(campaign).items()
            if key.startswith("approved_")
        }
    )
