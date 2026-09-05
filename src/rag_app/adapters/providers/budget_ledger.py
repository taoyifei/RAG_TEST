"""通过 SQLite 原子预留公开合成数据验收的累计出站预算。"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rag_app.adapters.providers.budget_errors import BudgetBlockedError
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.adapters.providers.budget_revision import (
    BudgetAuthorizationRevision,
    budget_payload_set_identity,
)
from rag_app.core.identifiers import canonical_sha256

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
__all__ = [
    "BudgetBlockedError",
    "BudgetCampaign",
    "BudgetRequest",
    "ProviderBudgetLedger",
    "safe_identifier",
    "summarize_attempts",
]


@dataclass(frozen=True)
class BudgetRequest:
    """一次请求的脱敏身份；不保存请求正文、Headers 或 Secret。"""

    provider: str
    operation: str
    request_identity: str
    payload_identity: str
    estimated_input_tokens: int
    retry_index: int = 0
    text_hashes: tuple[str, ...] = ()
    shape_identity: str | None = None


class ProviderBudgetLedger:
    """持久保存 campaign、attempt 和追加导入的历史使用记录。"""

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._read_only = read_only
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_budget_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    configuration TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE'
                );
                CREATE TABLE IF NOT EXISTS provider_budget_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_identity TEXT NOT NULL,
                    payload_identity TEXT NOT NULL,
                    retry_index INTEGER NOT NULL,
                    reserved INTEGER NOT NULL,
                    forwarded INTEGER NOT NULL DEFAULT 0,
                    locally_blocked INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    estimated_input_tokens INTEGER NOT NULL,
                    observed_tokens INTEGER,
                    request_id TEXT,
                    http_status INTEGER,
                    safe_code TEXT,
                    timestamp TEXT NOT NULL,
                    source_identity TEXT,
                    UNIQUE(campaign_id, source_identity)
                );
                CREATE INDEX IF NOT EXISTS provider_budget_campaign_attempts
                ON provider_budget_attempts(campaign_id);
                CREATE TABLE IF NOT EXISTS provider_budget_active_campaign (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    campaign_id TEXT NOT NULL,
                    activated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_budget_revisions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision_id TEXT UNIQUE NOT NULL,
                    campaign_id TEXT NOT NULL,
                    approval_reference TEXT UNIQUE NOT NULL,
                    configuration TEXT NOT NULL,
                    admin_session_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        database = (
            self.path.resolve().as_uri() + "?mode=ro"
            if self._read_only
            else str(self.path)
        )
        connection = sqlite3.connect(
            database, timeout=30, isolation_level=None, uri=self._read_only
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "BEGIN" if self._read_only else "BEGIN IMMEDIATE"
            )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_campaign(self, campaign: BudgetCampaign) -> None:
        """创建或核对同一授权；任何配置变化均拒绝，历史消费不清零。

        Args:
            campaign: 已批准且不可重新解释的授权配置。

        Returns:
            无返回值；只追加首次配置。

        """
        configuration = json.dumps(asdict(campaign), sort_keys=True)
        with self._transaction() as connection:
            previous = connection.execute(
                "SELECT configuration FROM provider_budget_campaigns "
                "WHERE campaign_id = ?",
                (campaign.campaign_id,),
            ).fetchone()
            if previous is not None:
                if previous["configuration"] != configuration:
                    raise BudgetBlockedError("CAMPAIGN_AUTHORIZATION_IMMUTABLE")
                return
            existing_authorizations = connection.execute(
                "SELECT configuration FROM provider_budget_campaigns"
            ).fetchall()
            if any(
                json.loads(row["configuration"])["authorization_id"]
                == campaign.authorization_id
                for row in existing_authorizations
            ):
                raise BudgetBlockedError("AUTHORIZATION_ALREADY_BOUND")
            connection.execute(
                "INSERT INTO provider_budget_campaigns "
                "(campaign_id, configuration, created_at) VALUES (?, ?, ?)",
                (campaign.campaign_id, configuration, _now()),
            )

    def campaign(self, campaign_id: str) -> BudgetCampaign:
        """读取已保存且未被新的进程重建的授权定义。

        Args:
            campaign_id: 已有的授权活动身份。

        Returns:
            已持久化的配置，不创建新授权。

        """
        with self._transaction() as connection:
            return self._campaign(connection, campaign_id)

    def base_campaign(self, campaign_id: str) -> BudgetCampaign:
        """读取不可变初始授权，用于核对配置而非实际出站预算。

        Args:
            campaign_id: 已有授权活动身份。

        Returns:
            保留原始限额和批准集的配置。

        """
        with self._transaction() as connection:
            return self._base_campaign(connection, campaign_id)

    def authorization_snapshot(self, campaign_id: str) -> dict[str, Any]:
        """读取审批所需身份、累计消费和过期状态，不激活或解密。

        Args:
            campaign_id: 需要准备后续明确批准的活动身份。

        Returns:
            管理员可复用的批准集哈希、前序身份与当前累计限额。

        """
        with self._transaction() as connection:
            base = self._base_campaign(connection, campaign_id)
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='provider_budget_revisions'"
            ).fetchone()
            row = (
                None
                if present is None
                else connection.execute(
                    "SELECT revision_id,configuration "
                    "FROM provider_budget_revisions "
                    "WHERE campaign_id=? ORDER BY sequence DESC LIMIT 1",
                    (campaign_id,),
                ).fetchone()
            )
            current = (
                base
                if row is None
                else BudgetAuthorizationRevision(
                    **json.loads(row["configuration"])
                ).effective_campaign(base)
            )
            reason = None
            try:
                self._campaign(connection, campaign_id)
            except BudgetBlockedError as error:
                reason = error.reason
            attempts = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM provider_budget_attempts "
                    "WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchall()
            ]
        return {
            "campaign_id": campaign_id,
            "authorization_id": base.authorization_id,
            "scope": base.scope,
            "payload_set_identity": budget_payload_set_identity(base),
            "latest_revision_id": None if row is None else row["revision_id"],
            "previous_revision_id_for_update": None
            if row is None
            else row["revision_id"],
            "authorization_valid": reason is None,
            "blocking_reason": reason,
            "stored_cumulative_limits": {
                name: value
                for name, value in asdict(current).items()
                if name.endswith(("limit", "limits"))
            },
            "usage": summarize_attempts(attempts),
            "providers": {
                provider: summarize_attempts(
                    [item for item in attempts if item["provider"] == provider]
                )
                for provider in sorted({item["provider"] for item in attempts})
            },
        }

    def apply_revision(
        self,
        revision: BudgetAuthorizationRevision,
        *,
        admin_session_id: str,
    ) -> None:
        """由已认证管理员边界追加修订，和并发预留共用写事务。

        本方法是受信 Python 接口。外部入口必须调用 approve_budget_revision
        验证管理员会话，不可将请求体字段当作 admin_session_id。

        Args:
            revision: 已明确批准的累计上限，不是新增一份余额。
            admin_session_id: 认证器返回的真实管理员 Session ID。

        Returns:
            无返回值；重放同一修订幂等，冲突拒绝。

        """
        self._assert_dispatch_allowed()
        revision.validate()
        if safe_identifier(admin_session_id) is None:
            raise BudgetBlockedError("BUDGET_ADMIN_SESSION_REQUIRED")
        encoded = json.dumps(asdict(revision), sort_keys=True)
        with self._transaction() as connection:
            binding = connection.execute(
                "SELECT campaign_id FROM provider_budget_active_campaign "
                "WHERE singleton=1"
            ).fetchone()
            if (
                binding is None
                or binding["campaign_id"] != revision.campaign_id
            ):
                raise BudgetBlockedError("CAMPAIGN_BINDING_REQUIRED")
            base = self._base_campaign(connection, revision.campaign_id)
            effective = revision.effective_campaign(base)
            existing = connection.execute(
                "SELECT configuration FROM provider_budget_revisions "
                "WHERE revision_id=?",
                (revision.revision_id,),
            ).fetchone()
            if existing is not None:
                if existing["configuration"] != encoded:
                    raise BudgetBlockedError("BUDGET_REVISION_CONFLICT")
                return
            previous = connection.execute(
                "SELECT revision_id FROM provider_budget_revisions "
                "WHERE campaign_id=? ORDER BY sequence DESC LIMIT 1",
                (revision.campaign_id,),
            ).fetchone()
            previous_id = None if previous is None else previous["revision_id"]
            if previous_id != revision.previous_revision_id:
                raise BudgetBlockedError("BUDGET_REVISION_CHAIN_MISMATCH")
            reused = connection.execute(
                "SELECT 1 FROM provider_budget_revisions "
                "WHERE approval_reference=?",
                (revision.approval_reference,),
            ).fetchone()
            if reused is not None:
                raise BudgetBlockedError(
                    "BUDGET_APPROVAL_REFERENCE_ALREADY_USED"
                )
            _assert_limits_cover_usage(connection, effective)
            connection.execute(
                "INSERT INTO provider_budget_revisions "
                "(revision_id,campaign_id,approval_reference,configuration,"
                "admin_session_id,recorded_at) VALUES (?,?,?,?,?,?)",
                (
                    revision.revision_id,
                    revision.campaign_id,
                    revision.approval_reference,
                    encoded,
                    admin_session_id,
                    _now(),
                ),
            )

    def activate_campaign(self, campaign_id: str) -> None:
        """持久绑定授权，使已运行的产品进程共同遵守预算。

        激活幂等且不可解绑、替换或清零；增加额度须有另行批准的迁移。

        Args:
            campaign_id: 已明确批准并保存的授权活动身份。

        Returns:
            无返回值；只追加首次绑定。

        """
        self._assert_dispatch_allowed()
        with self._transaction() as connection:
            self._campaign(connection, campaign_id)
            previous = connection.execute(
                "SELECT campaign_id FROM provider_budget_active_campaign "
                "WHERE singleton = 1"
            ).fetchone()
            if previous is not None:
                if previous["campaign_id"] != campaign_id:
                    raise BudgetBlockedError("ACTIVE_CAMPAIGN_IMMUTABLE")
                return
            connection.execute(
                "INSERT INTO provider_budget_active_campaign "
                "(singleton, campaign_id, activated_at) VALUES (1, ?, ?)",
                (campaign_id, _now()),
            )

    def active_campaign(self) -> BudgetCampaign | None:
        """读取每次发送都必须遵循的产品持久授权。

        Args:
            无参数；读取当前产品账本。

        Returns:
            当前授权，尚未绑定时返回 None。

        """
        self._assert_dispatch_allowed()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM provider_budget_active_campaign "
                "WHERE singleton = 1"
            ).fetchone()
            return (
                None
                if row is None
                else self._campaign(connection, row["campaign_id"])
            )

    def active_campaign_id(self) -> str | None:
        """只读绑定身份，允许管理员修订已过期授权而不允许出站。

        Args:
            无参数；读取当前账本绑定。

        Returns:
            绑定活动身份，未绑定为 None；恢复阻断仍拒绝。

        """
        self._assert_dispatch_allowed()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM provider_budget_active_campaign "
                "WHERE singleton=1"
            ).fetchone()
            return None if row is None else str(row["campaign_id"])

    def campaign_binding_time(self, campaign_id: str) -> str | None:
        """读取首次激活时间，历史重放不重复导入已受账本保护的新事件。

        Args:
            campaign_id: 当前已绑定活动。

        Returns:
            首绑时间；活动未绑定时为 None。

        """
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT activated_at FROM provider_budget_active_campaign "
                "WHERE singleton=1 AND campaign_id=?",
                (campaign_id,),
            ).fetchone()
            return None if row is None else str(row["activated_at"])

    def _assert_dispatch_allowed(self) -> None:
        if (self.path.parent / "provider-budget.restore-blocked").exists():
            raise BudgetBlockedError("BOUNDARY_RECONCILIATION_REQUIRED")

    @staticmethod
    def _base_campaign(
        connection: sqlite3.Connection, campaign_id: str
    ) -> BudgetCampaign:
        row = connection.execute(
            "SELECT configuration, status FROM provider_budget_campaigns "
            "WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None or row["status"] != "ACTIVE":
            raise BudgetBlockedError("CAMPAIGN_NOT_ACTIVE")
        configuration = json.loads(row["configuration"])
        for key in tuple(configuration):
            if key.startswith("approved_"):
                configuration[key] = tuple(configuration[key])
        return BudgetCampaign(**configuration)

    @classmethod
    def _campaign(
        cls, connection: sqlite3.Connection, campaign_id: str
    ) -> BudgetCampaign:
        base = cls._base_campaign(connection, campaign_id)
        # 只读旧账本也可诊断，不为新增表偷偷执行迁移。
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='provider_budget_revisions'"
        ).fetchone()
        if present is None:
            return base
        rows = connection.execute(
            "SELECT revision_id,configuration,admin_session_id "
            "FROM provider_budget_revisions WHERE campaign_id=? "
            "ORDER BY sequence",
            (campaign_id,),
        ).fetchall()
        previous_id = None
        effective = base
        for row in rows:
            revision = BudgetAuthorizationRevision(
                **json.loads(row["configuration"])
            )
            revision.validate(now=datetime.fromisoformat(revision.approved_at))
            if (
                revision.previous_revision_id != previous_id
                or revision.revision_id != row["revision_id"]
                or safe_identifier(row["admin_session_id"]) is None
            ):
                raise BudgetBlockedError("BUDGET_REVISION_CHAIN_MISMATCH")
            effective = revision.effective_campaign(base)
            previous_id = revision.revision_id
        if rows:
            revision.validate()
        return effective

    def reserve(
        self,
        campaign_id: str,
        *,
        authorization_id: str,
        scope: str,
        step_id: str,
        request: BudgetRequest,
    ) -> str:
        """单事务检查所有累计限制并预留；拒绝也保留本地 attempt。

        Args:
            campaign_id: 持久授权活动身份。
            authorization_id: 必须与批准记录一致的授权身份。
            scope: 当前调用的批准范围。
            step_id: 当前阶段的安全标识符。
            request: 不含正文或密钥的请求描述。

        Returns:
            成功预留的 attempt 身份。

        """
        self._assert_dispatch_allowed()
        _validate_request(step_id, request)
        blocked: BudgetBlockedError | None = None
        attempt_id = "attempt-" + uuid.uuid4().hex
        with self._transaction() as connection:
            campaign = self._campaign(connection, campaign_id)
            if authorization_id != campaign.authorization_id:
                blocked = BudgetBlockedError("AUTHORIZATION_ID_MISMATCH")
            elif scope != campaign.scope:
                blocked = BudgetBlockedError("AUTHORIZATION_SCOPE_MISMATCH")
            elif not _payload_approved(campaign, request):
                blocked = BudgetBlockedError("PAYLOAD_NOT_APPROVED")
            elif (
                campaign.approved_request_identities
                and request.request_identity
                not in campaign.approved_request_identities
            ):
                blocked = BudgetBlockedError("REQUEST_IDENTITY_NOT_APPROVED")
            else:
                blocked = _budget_failure(
                    connection, campaign, request, step_id
                )
            connection.execute(
                "INSERT INTO provider_budget_attempts "
                "(attempt_id, campaign_id, step_id, provider, operation, "
                "request_identity, payload_identity, retry_index, reserved, "
                "locally_blocked, status, estimated_input_tokens, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    campaign_id,
                    step_id,
                    request.provider,
                    request.operation,
                    request.request_identity,
                    request.payload_identity,
                    request.retry_index,
                    int(blocked is None),
                    int(blocked is not None),
                    "RESERVED" if blocked is None else blocked.reason,
                    request.estimated_input_tokens,
                    _now(),
                ),
            )
        if blocked is not None:
            raise blocked
        return attempt_id

    def mark_forwarded(self, attempt_id: str) -> None:
        """在调用实际 Transport 前落盘，未知结果仍占用预算。

        Args:
            attempt_id: 已成功预留的请求尝试身份。

        Returns:
            无返回值；标记即将发送且不可自动退还的请求。

        """
        self._assert_dispatch_allowed()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT campaign_id FROM provider_budget_attempts "
                "WHERE attempt_id=? AND status='RESERVED'",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise BudgetBlockedError("ATTEMPT_NOT_RESERVED")
            self._campaign(connection, row["campaign_id"])
            cursor = connection.execute(
                "UPDATE provider_budget_attempts SET forwarded = 1, "
                "status = 'FORWARDED' WHERE attempt_id = ? "
                "AND status = 'RESERVED'",
                (attempt_id,),
            )
            if cursor.rowcount != 1:
                raise BudgetBlockedError("ATTEMPT_NOT_RESERVED")

    def finish(
        self,
        attempt_id: str,
        *,
        status: str,
        observed_tokens: int | None = None,
        request_id: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """保存安全 HTTP 终态与 usage；未知 usage 保留 NULL。

        Args:
            attempt_id: 已标记转发的请求尝试身份。
            status: 内部生成的稳定状态。
            observed_tokens: 校验后的供应商 usage；无值时保持未知。
            request_id: 可选且经过安全过滤的供应商请求身份。
            http_status: 供应商返回的 HTTP 状态。

        Returns:
            无返回值；保存有限的安全诊断字段。

        """
        if not _SAFE_IDENTIFIER.fullmatch(status):
            raise ValueError("无效预算状态。")
        if observed_tokens is not None and (
            isinstance(observed_tokens, bool) or observed_tokens < 0
        ):
            raise ValueError("observed_tokens 必须是非负整数或 unknown。")
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_budget_attempts SET status = ?, "
                "observed_tokens = ?, request_id = ?, http_status = ?, "
                "safe_code = ? WHERE attempt_id = ? AND forwarded = 1",
                (
                    status,
                    observed_tokens,
                    safe_identifier(request_id),
                    http_status,
                    None if http_status is None else f"HTTP_{http_status}",
                    attempt_id,
                ),
            )

    def mark_locally_blocked(self, attempt_id: str) -> None:
        """验收注入在转发前拒绝，释放预留并与供应商 HTTP 分开统计。

        Args:
            attempt_id: 尚未转发的预留身份。

        Returns:
            无返回值；保留本地阻断记录。

        """
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_budget_attempts SET locally_blocked = 1, "
                "reserved = 0, status = 'LOCALLY_BLOCKED' "
                "WHERE attempt_id = ? AND status = 'RESERVED'",
                (attempt_id,),
            )

    def import_history(
        self,
        campaign_id: str,
        *,
        source_identity: str,
        events: list[dict[str, Any]],
    ) -> None:
        """按来源与事件唯一身份追加历史；重复导入幂等且不覆盖旧行。

        events 只接受事件身份、Provider、操作、是否转发与两类 Token。
        调用方应从权威数据库脱敏读取，不导入正文。

        Args:
            campaign_id: 需要累加历史的已批准活动身份。
            source_identity: 稳定历史来源的 SHA256。
            events: 具有唯一事件身份的安全使用记录。

        Returns:
            无返回值；重复数据幂等，冲突数据拒绝。

        """
        if not _HASH.fullmatch(source_identity):
            raise ValueError("历史来源只保存 SHA256 身份。")
        with self._transaction() as connection:
            self._campaign(connection, campaign_id)
            for event in events:
                _import_event(connection, campaign_id, source_identity, event)

    def attempts(self, campaign_id: str) -> list[dict[str, Any]]:
        """读取不含请求原文或 Secret 的逐次审计。

        Args:
            campaign_id: 要读取的授权活动身份。

        Returns:
            按时间排序的安全 attempt 记录。

        """
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_budget_attempts WHERE campaign_id = ? "
                "ORDER BY timestamp, attempt_id",
                (campaign_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, campaign_id: str) -> dict[str, Any]:
        """分别报告 HTTP、预留、拒绝、估算和实际可观测 Token。

        Args:
            campaign_id: 要汇总的授权活动身份。

        Returns:
            累计和各 Provider 的脱敏统计。

        """
        attempts = self.attempts(campaign_id)
        campaign = self.campaign(campaign_id)
        summary = summarize_attempts(attempts)
        summary.update(
            {
                "campaign_id": campaign_id,
                "authorization_id": campaign.authorization_id,
                "request_limit": campaign.request_limit,
                "estimated_token_limit": campaign.estimated_token_limit,
                "provider_request_limits": dict(
                    campaign.provider_request_limits
                ),
                "provider_token_limits": dict(campaign.provider_token_limits),
                "step_request_limits": dict(campaign.step_request_limits),
                "providers": {
                    provider: summarize_attempts(
                        [row for row in attempts if row["provider"] == provider]
                    )
                    for provider in sorted(
                        {row["provider"] for row in attempts}
                    )
                },
                "steps": {
                    step: summarize_attempts(
                        [row for row in attempts if row["step_id"] == step]
                    )
                    for step in sorted({row["step_id"] for row in attempts})
                },
            }
        )
        return summary

    def minimum_additional(
        self, campaign_id: str, attempt_id: str
    ) -> dict[str, int]:
        """根据真实预算拒绝记录和当前累计值只读计算恢复所需的最小额度。

        Args:
            campaign_id: 被拒绝请求所属的持久授权活动身份。
            attempt_id: 已记录的本地预算拒绝尝试身份。

        Returns:
            请求、估算 Token、Provider 和阶段的正数缺口；非预算拒绝为空。

        """
        reader = ProviderBudgetLedger(self.path, read_only=True)
        with reader._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_budget_attempts "
                "WHERE campaign_id = ? AND attempt_id = ?",
                (campaign_id, attempt_id),
            ).fetchone()
            if row is None:
                raise ValueError("预算尝试记录不存在。")
            if not row["locally_blocked"] or row["status"] != "BLOCKED_BUDGET":
                return {}
            request = BudgetRequest(
                provider=row["provider"],
                operation=row["operation"],
                request_identity=row["request_identity"],
                payload_identity=row["payload_identity"],
                estimated_input_tokens=row["estimated_input_tokens"],
                retry_index=row["retry_index"],
            )
            failure = _budget_failure(
                connection,
                self._campaign(connection, campaign_id),
                request,
                row["step_id"],
            )
        return {} if failure is None else failure.minimum_additional


def safe_identifier(value: object) -> str | None:
    """仅保留长度有界的机器标识符，不回显自由文本诊断。

    Args:
        value: 未信任的候选标识符。

    Returns:
        安全标识符或 None。

    """
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_request(step_id: str, request: BudgetRequest) -> None:
    if any(
        not _SAFE_IDENTIFIER.fullmatch(value)
        for value in (step_id, request.provider, request.operation)
    ):
        raise ValueError("请求审计只接受安全标识符。")
    if any(
        not _HASH.fullmatch(value)
        for value in (request.request_identity, request.payload_identity)
    ):
        raise ValueError("请求身份必须是 SHA256。")
    if request.estimated_input_tokens < 0 or request.retry_index < 0:
        raise ValueError("请求估算与重试次数不可为负数。")


def _payload_approved(campaign: BudgetCampaign, request: BudgetRequest) -> bool:
    if request.payload_identity in campaign.approved_payload_hashes:
        return True
    return bool(
        request.text_hashes
        and request.shape_identity in campaign.approved_request_shape_hashes
        and all(
            value in campaign.approved_text_hashes
            for value in request.text_hashes
        )
    )


def _budget_failure(
    connection: sqlite3.Connection,
    campaign: BudgetCampaign,
    request: BudgetRequest,
    step_id: str,
) -> BudgetBlockedError | None:
    rows = connection.execute(
        "SELECT provider, step_id, estimated_input_tokens "
        "FROM provider_budget_attempts WHERE campaign_id = ? AND reserved = 1",
        (campaign.campaign_id,),
    ).fetchall()
    total_tokens = sum(row["estimated_input_tokens"] for row in rows)
    provider_rows = [row for row in rows if row["provider"] == request.provider]
    provider_tokens = sum(
        row["estimated_input_tokens"] for row in provider_rows
    )
    needs = {
        "requests": len(rows) + 1 - campaign.request_limit,
        "estimated_input_tokens": (
            total_tokens
            + request.estimated_input_tokens
            - campaign.estimated_token_limit
        ),
        "provider_requests": len(provider_rows)
        + 1
        - campaign.provider_request_limits.get(
            request.provider, campaign.request_limit
        ),
        "provider_estimated_input_tokens": (
            provider_tokens
            + request.estimated_input_tokens
            - campaign.provider_token_limits.get(
                request.provider, campaign.estimated_token_limit
            )
        ),
        "step_requests": sum(row["step_id"] == step_id for row in rows)
        + 1
        - campaign.step_request_limits.get(step_id, campaign.request_limit),
    }
    additional = {key: value for key, value in needs.items() if value > 0}
    return (
        BudgetBlockedError("BLOCKED_BUDGET", additional) if additional else None
    )


def _assert_limits_cover_usage(
    connection: sqlite3.Connection, campaign: BudgetCampaign
) -> None:
    rows = connection.execute(
        "SELECT provider,step_id,estimated_input_tokens "
        "FROM provider_budget_attempts WHERE campaign_id=? AND reserved=1",
        (campaign.campaign_id,),
    ).fetchall()
    if (
        len(rows) > campaign.request_limit
        or sum(row["estimated_input_tokens"] for row in rows)
        > campaign.estimated_token_limit
    ):
        raise BudgetBlockedError("BUDGET_REVISION_BELOW_CONSUMPTION")
    for provider in {row["provider"] for row in rows}:
        selected = [row for row in rows if row["provider"] == provider]
        if len(selected) > campaign.provider_request_limits.get(
            provider, campaign.request_limit
        ) or sum(
            row["estimated_input_tokens"] for row in selected
        ) > campaign.provider_token_limits.get(
            provider, campaign.estimated_token_limit
        ):
            raise BudgetBlockedError("BUDGET_REVISION_BELOW_CONSUMPTION")
    for step in {row["step_id"] for row in rows}:
        if sum(row["step_id"] == step for row in rows) > (
            campaign.step_request_limits.get(step, campaign.request_limit)
        ):
            raise BudgetBlockedError("BUDGET_REVISION_BELOW_CONSUMPTION")


def _import_event(
    connection: sqlite3.Connection,
    campaign_id: str,
    source_identity: str,
    event: dict[str, Any],
) -> None:
    source_event = canonical_sha256(
        {"source": source_identity, "event_id": event["event_id"]}
    )
    forwarded = bool(event["forwarded"])
    estimated = event["estimated_input_tokens"]
    observed = event.get("observed_tokens")
    if (
        type(estimated) is not int
        or estimated < 0
        or (
            observed is not None and (type(observed) is not int or observed < 0)
        )
    ):
        raise ValueError("历史 Token 值无效。")
    provider = safe_identifier(event["provider"])
    operation = safe_identifier(event["operation"])
    if provider is None or operation is None:
        raise ValueError("历史 Provider 或 operation 标识符无效。")
    previous = connection.execute(
        "SELECT provider, operation, forwarded, estimated_input_tokens, "
        "observed_tokens FROM provider_budget_attempts "
        "WHERE campaign_id = ? AND source_identity = ?",
        (campaign_id, source_event),
    ).fetchone()
    expected = (
        provider,
        operation,
        int(forwarded),
        estimated,
        observed if forwarded else None,
    )
    if previous is not None and tuple(previous) != expected:
        raise BudgetBlockedError("HISTORICAL_EVENT_CONFLICT")
    connection.execute(
        "INSERT OR IGNORE INTO provider_budget_attempts "
        "(attempt_id, campaign_id, step_id, provider, operation, "
        "request_identity, payload_identity, retry_index, reserved, forwarded, "
        "locally_blocked, status, estimated_input_tokens, observed_tokens, "
        "timestamp, source_identity) "
        "VALUES (?, ?, 'historical', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "history-" + source_event,
            campaign_id,
            provider,
            operation,
            source_event,
            source_event,
            int(forwarded),
            int(forwarded),
            int(not forwarded),
            safe_identifier(event.get("status"))
            or (
                "HISTORICAL_FORWARDED"
                if forwarded
                else "HISTORICAL_LOCAL_BLOCK"
            ),
            estimated,
            observed if forwarded else None,
            _now(),
            source_event,
        ),
    )


def summarize_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """对脱敏 attempt 子集使用与累计账本一致的统计语义。

    Args:
        attempts: 从账本读取的安全尝试子集。

    Returns:
        明确区分未知 usage、估算预留与实际转发的统计。

    """
    forwarded = [row for row in attempts if row["forwarded"]]
    observed = [
        row["observed_tokens"]
        for row in forwarded
        if row["observed_tokens"] is not None
    ]
    return {
        "total": len(attempts),
        "reserved": sum(row["reserved"] for row in attempts),
        "forwarded": len(forwarded),
        "locally_blocked": sum(row["locally_blocked"] for row in attempts),
        "estimated_input_tokens": sum(
            row["estimated_input_tokens"] for row in attempts if row["reserved"]
        ),
        "observed_tokens": sum(observed) if observed else None,
        "observed_usage_status": (
            "known"
            if len(observed) == len(forwarded) and forwarded
            else "unknown"
        ),
        "unknown_usage_attempts": len(forwarded) - len(observed),
        "unknown_forwarding_attempts": sum(
            row.get("status", "").upper()
            in {
                "RESERVED",
                "FORWARDED",
                "TRANSPORT_ERROR",
                "UNKNOWN",
                "UNKNOWN_RETRY",
                "UNKNOWN_VALIDATION_FORWARDING",
                "CONNECT_TIMEOUT",
                "READ_TIMEOUT",
                "CONNECTION_ERROR",
            }
            for row in attempts
            if row["reserved"]
        ),
        "locally_blocked_estimated_tokens": sum(
            row["estimated_input_tokens"]
            for row in attempts
            if row["locally_blocked"]
        ),
    }
