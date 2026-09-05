"""在明确的离线维护窗口绑定现有产品历史与持久 Provider 授权。"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
    summarize_attempts,
)
from rag_app.core.identifiers import canonical_sha256

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_LOCAL_CATEGORIES = frozenset(
    {"invalid_configuration", "credential_unavailable", "locally_blocked"}
)


@contextmanager
def provider_request_lease(
    data_dir: Path, *, offline_restore: bool = False
) -> Iterator[None]:
    """与首绑维护互斥；只覆盖 HTTP，不能据此宣称业务事件已全部落盘。

    Args:
        data_dir: 当前产品的数据目录。
        offline_restore: 仅受信固定内存响应器在恢复场景使用，不解锁真实 HTTP。

    Returns:
        限制维护与出站并发的短租约。

    """
    if not data_dir.exists():
        yield
        return
    with _boundary_lock(data_dir, exclusive=False):
        if (
            not offline_restore
            and (data_dir / "provider-budget.restore-blocked").exists()
        ):
            raise BudgetBlockedError("BOUNDARY_RECONCILIATION_REQUIRED")
        if (data_dir / "provider-budget.initializing").exists():
            raise BudgetBlockedError("BUDGET_INITIALIZATION_IN_PROGRESS")
        yield


@contextmanager
def budget_initialization(data_dir: Path) -> Iterator[None]:
    """封闭首次绑定期间的新出站，检测在途 HTTP 后拒绝而不盲目重试。

    调用方必须先确认所有 Provider 进程停止，以包含 HTTP 结束后才保存的
    Probe/SDK 业务事件；文件锁与标记本身不能证明旧版本进程已经停止。

    Args:
        data_dir: 已进入离线维护窗口的产品数据目录。

    Returns:
        仅成功绑定才移除本次初始化标记的上下文管理器。

    """
    marker = data_dir / "provider-budget.initializing"
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        raise BudgetBlockedError("BUDGET_INITIALIZATION_IN_PROGRESS") from None
    with _boundary_lock(data_dir, exclusive=True):
        yield
    marker.unlink()


@contextmanager
def _boundary_lock(data_dir: Path, *, exclusive: bool) -> Iterator[None]:
    lock_path = data_dir / "provider-budget.lock"
    with lock_path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            _lock(stream, exclusive=exclusive)
        except OSError:
            raise BudgetBlockedError("BLOCKED_INFLIGHT") from None
        try:
            yield
        finally:
            _unlock(stream)


def _lock(stream: BinaryIO, *, exclusive: bool) -> None:
    if sys.platform == "win32":
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def bind_existing_product_campaign(
    data_dir: Path,
    campaign: BudgetCampaign,
    *,
    maintenance_confirmed: bool = False,
) -> dict[str, Any]:
    """只读旧库安全事件，逐次追加历史后激活同目录的不可重置账本。

    Args:
        data_dir: 现有 universal-rag.sqlite3 所在产品目录。
        campaign: 已经明确批准的公开请求集和累计限制。
        maintenance_confirmed: 调用方已确认 Provider 进程全部停止。

    Returns:
        导入后的累计安全账本摘要，估算与实际观测保持分列。

    """
    if not maintenance_confirmed:
        raise BudgetBlockedError("BLOCKED_MAINTENANCE_REQUIRED")
    with budget_initialization(data_dir):
        ledger = ProviderBudgetLedger(data_dir / "provider-budget.sqlite3")
        active = ledger.active_campaign_id()
        events = _historical_events(data_dir / "universal-rag.sqlite3")
        source_identity = canonical_sha256(
            {
                "source": "provider_operation_events",
                "campaign_id": campaign.campaign_id,
            }
        )
        if active is not None:
            ledger.create_campaign(campaign)
            cutoff = ledger.campaign_binding_time(campaign.campaign_id)
            if cutoff is None:
                raise BudgetBlockedError("ACTIVE_CAMPAIGN_IMMUTABLE")
            ledger.import_history(
                campaign.campaign_id,
                source_identity=source_identity,
                events=[
                    event
                    for event in events
                    if _timestamp(event["occurred_at"]) <= _timestamp(cutoff)
                ],
            )
            ledger.activate_campaign(campaign.campaign_id)
            summary = ledger.summary(campaign.campaign_id)
        else:
            ledger.create_campaign(campaign)
            ledger.import_history(
                campaign.campaign_id,
                source_identity=source_identity,
                events=events,
            )
            ledger.activate_campaign(campaign.campaign_id)
            summary = ledger.summary(campaign.campaign_id)
    return summary


def read_product_budget_history(data_dir: Path) -> dict[str, Any]:
    """只读来源去重的旧消费，未首绑时也保留真实累计数字。

    Args:
        data_dir: 既有产品 SQLite 目录，只 SELECT 安全事件列。

    Returns:
        与账本相同统计字段及 Provider 子集；不创建或修改任何文件。

    """
    events = _historical_events(data_dir / "universal-rag.sqlite3")
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        previous = unique.setdefault(event["event_id"], event)
        if previous != event:
            raise BudgetBlockedError("HISTORICAL_EVENT_CONFLICT")
    attempts = [
        {
            **event,
            "reserved": int(event["forwarded"]),
            "locally_blocked": int(not event["forwarded"]),
            "status": "HISTORICAL_FORWARDED"
            if "status" not in event and event["forwarded"]
            else event.get("status", "HISTORICAL_LOCAL_BLOCK"),
        }
        for event in unique.values()
    ]
    return {
        **summarize_attempts(attempts),
        "source": "provider_operation_events_read_only_deduplicated",
        "validation_coverage": "RECONCILED_OR_RESERVED_UNKNOWN",
        "unmatched_validation_attempts": sum(
            row["event_id"].startswith("validation:") for row in attempts
        ),
        "providers": {
            provider: summarize_attempts(
                [row for row in attempts if row["provider"] == provider]
            )
            for provider in sorted({row["provider"] for row in attempts})
        },
    }


def _historical_events(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT e.event_id, e.connection_id, e.occurred_at, "
            "c.provider_type, e.operation, "
            "e.status_category, "
            "e.estimated_tokens, e.observed_tokens, e.retry_count, e.cache_hit "
            "FROM provider_operation_events e LEFT JOIN provider_connections c "
            "ON c.connection_id=e.connection_id "
            "ORDER BY e.occurred_at, e.event_id"
        ).fetchall()
        unmatched, mock_events = _unmatched_validations(connection, rows)
    finally:
        connection.close()
    events: list[dict[str, Any]] = []
    for row in rows:
        if row["cache_hit"] or row["event_id"] in mock_events:
            continue
        if row["provider_type"] not in {"jina", "aliyun-model-studio"}:
            raise BudgetBlockedError("HISTORICAL_PROVIDER_IDENTITY_UNKNOWN")
        forwarded = row["status_category"] not in _LOCAL_CATEGORIES
        retries = int(row["retry_count"]) if forwarded else 0
        if retries < 0:
            raise BudgetBlockedError("HISTORICAL_RETRY_COUNT_INVALID")
        events.extend(
            {
                "event_id": f"{row['event_id']}:attempt:{retry}",
                "occurred_at": row["occurred_at"],
                "provider": "jina"
                if row["provider_type"] == "jina"
                else "aliyun",
                "operation": row["operation"],
                "forwarded": forwarded,
                "estimated_input_tokens": row["estimated_tokens"],
                "observed_tokens": row["observed_tokens"]
                if retry == retries
                else None,
                "status": row["status_category"]
                if retry == retries
                else "UNKNOWN_RETRY",
            }
            for retry in range(retries + 1)
        )
    return events + unmatched


def _unmatched_validations(
    connection: sqlite3.Connection, events: list[sqlite3.Row]
) -> tuple[list[dict[str, Any]], set[str]]:
    """检查两事务验证审计的覆盖；只有唯一后续同值事件才消除重复。"""
    validations = connection.execute(
        "SELECT v.validation_id,v.connection_id,v.operation,v.started_at,"
        "v.finished_at,v.status,v.http_category,v.estimated_tokens,"
        "v.observed_tokens,v.validation_mode,c.provider_type,"
        "json_extract(v.diagnostics_json,'$.request_dispatched') AS dispatched "
        "FROM provider_validation_runs v LEFT JOIN provider_connections c "
        "ON c.connection_id=v.connection_id "
        "ORDER BY v.finished_at,v.validation_id"
    ).fetchall()
    unmatched = []
    consumed: set[str] = set()
    mock_events: set[str] = set()
    for index, validation in enumerate(validations):
        provider = validation["provider_type"]
        if provider not in {"jina", "aliyun-model-studio"}:
            raise BudgetBlockedError("HISTORICAL_PROVIDER_IDENTITY_UNKNOWN")
        category = (
            "SUCCESS"
            if validation["status"] == "succeeded"
            else validation["http_category"]
        )
        candidates = [
            event
            for event in events
            if event["event_id"] not in consumed
            and _validation_event_matches(validation, event, category)
            and not any(
                later["connection_id"] == validation["connection_id"]
                and later["operation"] == validation["operation"]
                and _timestamp(later["started_at"])
                <= _timestamp(event["occurred_at"])
                for later in validations[index + 1 :]
            )
        ]
        if len(candidates) > 1:
            raise BudgetBlockedError(
                "HISTORICAL_VALIDATION_CORRELATION_AMBIGUOUS"
            )
        if candidates:
            consumed.add(candidates[0]["event_id"])
            if validation["validation_mode"] == "mock":
                mock_events.add(candidates[0]["event_id"])
            continue
        if validation["validation_mode"] == "mock":
            continue
        forwarded = (
            validation["dispatched"] != 0 and category not in _LOCAL_CATEGORIES
        )
        unmatched.append(
            {
                "event_id": (
                    f"validation:{validation['validation_id']}:attempt:0"
                ),
                "occurred_at": validation["finished_at"],
                "provider": "jina" if provider == "jina" else "aliyun",
                "operation": validation["operation"],
                "forwarded": forwarded,
                "estimated_input_tokens": validation["estimated_tokens"],
                "observed_tokens": validation["observed_tokens"]
                if forwarded
                else None,
                "status": "UNKNOWN_VALIDATION_FORWARDING"
                if forwarded
                else "HISTORICAL_LOCAL_BLOCK",
            }
        )
    return unmatched, mock_events


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("历史时间必须带时区。")
        return result
    except (TypeError, ValueError):
        raise BudgetBlockedError("HISTORICAL_TIMESTAMP_INVALID") from None


def _validation_event_matches(
    validation: sqlite3.Row, event: sqlite3.Row, category: str
) -> bool:
    return (
        event["connection_id"] == validation["connection_id"]
        and event["operation"] == validation["operation"]
        and event["status_category"] == category
        and event["estimated_tokens"] == validation["estimated_tokens"]
        and event["observed_tokens"] == validation["observed_tokens"]
        and event["retry_count"] == 0
        and not event["cache_hit"]
        and _timestamp(event["occurred_at"])
        >= _timestamp(validation["finished_at"])
    )
