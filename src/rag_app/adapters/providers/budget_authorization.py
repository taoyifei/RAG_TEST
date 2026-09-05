"""在明确的离线维护窗口绑定现有产品历史与持久 Provider 授权。"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
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
def provider_request_lease(data_dir: Path) -> Iterator[None]:
    """与首绑维护互斥；只覆盖 HTTP，不能据此宣称业务事件已全部落盘。

    Args:
        data_dir: 当前产品的数据目录。

    Returns:
        限制维护与出站并发的短租约。

    """
    if not data_dir.exists():
        yield
        return
    with _boundary_lock(data_dir, exclusive=False):
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
        active = ledger.active_campaign()
        if active is not None:
            ledger.create_campaign(campaign)
            ledger.activate_campaign(campaign.campaign_id)
            summary = ledger.summary(campaign.campaign_id)
        else:
            events = _historical_events(data_dir / "universal-rag.sqlite3")
            ledger.create_campaign(campaign)
            ledger.import_history(
                campaign.campaign_id,
                source_identity=canonical_sha256(
                    {
                        "source": "provider_operation_events",
                        "campaign_id": campaign.campaign_id,
                    }
                ),
                events=events,
            )
            ledger.activate_campaign(campaign.campaign_id)
            summary = ledger.summary(campaign.campaign_id)
    return summary


def _historical_events(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT e.event_id, c.provider_type, e.operation, "
            "e.status_category, "
            "e.estimated_tokens, e.observed_tokens, e.retry_count, e.cache_hit "
            "FROM provider_operation_events e JOIN provider_connections c "
            "ON c.connection_id=e.connection_id "
            "ORDER BY e.occurred_at, e.event_id"
        ).fetchall()
    finally:
        connection.close()
    events: list[dict[str, Any]] = []
    for row in rows:
        if row["cache_hit"]:
            continue
        forwarded = row["status_category"] not in _LOCAL_CATEGORIES
        retries = int(row["retry_count"]) if forwarded else 0
        if retries < 0:
            raise BudgetBlockedError("HISTORICAL_RETRY_COUNT_INVALID")
        events.extend(
            {
                "event_id": f"{row['event_id']}:attempt:{retry}",
                "provider": "jina"
                if row["provider_type"] == "jina"
                else "aliyun",
                "operation": row["operation"],
                "forwarded": forwarded,
                "estimated_input_tokens": row["estimated_tokens"],
                "observed_tokens": row["observed_tokens"]
                if retry == retries
                else None,
            }
            for retry in range(retries + 1)
        )
    return events
