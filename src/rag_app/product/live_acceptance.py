"""P11 公开合成验收的持久阶段调度；发布入口与 pytest 共用。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from rag_app.adapters.providers.budget_ledger import summarize_attempts
from rag_app.core.identifiers import canonical_sha256

STEPS = (
    "config_check",
    "aliyun_document_canary",
    "aliyun_query_canary",
    "jina_connection",
    "dual_index",
    "primary_query",
    "standby_failover",
    "recovery",
    "citation_quality",
    "final_report",
)
DEPENDENCIES = {
    "config_check": (),
    "aliyun_document_canary": ("config_check",),
    "aliyun_query_canary": ("aliyun_document_canary",),
    "jina_connection": ("config_check",),
    "dual_index": ("aliyun_query_canary", "jina_connection"),
    "primary_query": ("dual_index",),
    "standby_failover": ("primary_query",),
    "recovery": ("standby_failover",),
    "citation_quality": ("recovery",),
    "final_report": (),
}


@dataclass(frozen=True)
class StepResult:
    """单阶段安全证据，不接收供应商正文或密钥。"""

    status: str
    reason: str
    evidence: dict[str, object] = field(default_factory=dict)


class AcceptanceBackend(Protocol):
    """执行器必须独立证明自己的真实证据来源。"""

    def identity(self, step: str) -> str:
        """返回当前阶段配置、模型和凭据版本的非 Secret 身份。

        Args:
            step: 待执行阶段。

        Returns:
            当前阶段身份摘要。

        """
        ...

    def execute(self, step: str) -> StepResult:
        """执行一个阶段并返回可审计状态。

        Args:
            step: 选中的验收阶段。

        Returns:
            安全执行证据。

        """
        ...

    def evidence_is_current(
        self, step: str, record: Mapping[str, object]
    ) -> bool:
        """核验成功证据的时效、操作及授权，身份哈希本身不够。

        Args:
            step: 要复用的阶段。
            record: 既有成功记录。

        Returns:
            真实证据仍在有效期及批准范围内时为真。

        """
        ...

    def close(self) -> None:
        """释放执行器拥有的资源。

        Args:
            无参数；使用当前执行器。

        Returns:
            无返回值。

        """
        ...

    def budget_snapshot(self) -> dict[str, object]:
        """只读账本，不创建 campaign 或读取任何密钥。

        Args:
            无参数；读取已配置账本。

        Returns:
            脱敏累计记录。

        """
        ...

    def bind_campaign(self) -> StepResult:
        """在明确维护窗口绑定用户已批准的累计预算。

        Args:
            无参数；使用显式配置中的授权与上限。

        Returns:
            本地绑定结果；不调用供应商。

        """
        ...


class AcceptanceState:
    """只追加阶段记录，重启不会抹去失败、预算或历史证据。"""

    def __init__(self, path: Path, campaign_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.campaign_id = campaign_id
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS acceptance_steps ("
                "sequence INTEGER PRIMARY KEY, campaign TEXT NOT NULL, "
                "step TEXT NOT NULL, identity TEXT NOT NULL, "
                "status TEXT NOT NULL, reason TEXT NOT NULL, "
                "evidence TEXT NOT NULL, timestamp TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS acceptance_resources ("
                "campaign TEXT NOT NULL, name TEXT NOT NULL, value TEXT "
                "NOT NULL, PRIMARY KEY(campaign, name))"
            )

    def record(
        self, step: str, identity: str, result: StepResult
    ) -> dict[str, object]:
        """追加已实际执行、失败或受阻的阶段。

        Args:
            step: 验收阶段。
            identity: 当前适用配置身份。
            result: 实际执行的安全证据。

        Returns:
            已保存记录的摘要。

        """
        if result.status not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
            raise ValueError("不支持的验收状态。")
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO acceptance_steps(campaign,step,identity,status,"
                "reason,evidence,timestamp) VALUES(?,?,?,?,?,?,?)",
                (
                    self.campaign_id,
                    step,
                    identity,
                    result.status,
                    result.reason,
                    json.dumps(result.evidence, ensure_ascii=False),
                    timestamp,
                ),
            )
        return {
            "status": result.status,
            "reason": result.reason,
            "evidence": result.evidence,
            "identity": identity,
            "timestamp": timestamp,
            "provenance": "本次执行",
        }

    def latest(self, step: str) -> dict[str, object] | None:
        """读取同 campaign 最近记录，不跨授权复用。

        Args:
            step: 需要读取的阶段。

        Returns:
            最近记录；不存在时为空。

        """
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT status,reason,evidence,identity,timestamp FROM "
                "acceptance_steps WHERE campaign=? AND step=? "
                "ORDER BY sequence DESC LIMIT 1",
                (self.campaign_id, step),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "reason": row[1],
            "evidence": json.loads(row[2]),
            "identity": row[3],
            "timestamp": row[4],
            "provenance": "有效复用",
        }

    def resource(self, name: str, value: str | None = None) -> str | None:
        """保存验收独占项目、KB 和作业引用，支持幂等恢复。

        Args:
            name: 资源引用名称。
            value: 可选的新引用；空值仅作读取。

        Returns:
            当前资源引用；不存在时为空。

        """
        with sqlite3.connect(self.path) as db:
            if value is not None:
                db.execute(
                    "INSERT INTO acceptance_resources VALUES(?,?,?) "
                    "ON CONFLICT(campaign,name) DO UPDATE "
                    "SET value=excluded.value",
                    (self.campaign_id, name, value),
                )
            row = db.execute(
                "SELECT value FROM acceptance_resources "
                "WHERE campaign=? AND name=?",
                (self.campaign_id, name),
            ).fetchone()
        return None if row is None else str(row[0])


def _selected_steps(steps: Sequence[str] | None) -> set[str]:
    selected = (
        set(STEPS)
        if steps is None
        else {part.strip() for item in steps for part in item.split(",")}
    )
    if selected - set(STEPS):
        raise ValueError(
            "未知 Live 阶段：" + ",".join(sorted(selected - set(STEPS)))
        )
    return selected | {"config_check", "final_report"}


def _dependencies_ready(
    step: str, records: Mapping[str, Mapping[str, object]]
) -> bool:
    for name in DEPENDENCIES[step]:
        record = records[name]
        if record["status"] == "PASS":
            continue
        if name == "config_check" and _own_connection_ready(step, record):
            continue
        return False
    return True


def _own_connection_ready(step: str, record: Mapping[str, object]) -> bool:
    key = {
        "jina_connection": "jina_connection_id",
        "aliyun_document_canary": "aliyun_connection_id",
    }.get(step)
    evidence = record.get("evidence")
    if (
        key is None
        or record["status"] == "FAIL"
        or not isinstance(evidence, dict)
    ):
        return False
    connections = evidence.get("connections")
    if not isinstance(connections, dict):
        return False
    connection = connections.get(key)
    return (
        evidence.get("campaign_binding") == "PASS"
        and isinstance(connection, dict)
        and connection.get("status") == "PASS"
    )


def run_acceptance(
    config: Mapping[str, object] | None,
    *,
    steps: Sequence[str] | None = None,
    resume: bool = True,
    live: bool = False,
    backend: AcceptanceBackend | None = None,
) -> dict[str, object]:
    """按现有授权续跑选中阶段；未运行的发布门始终保持未完成。

    Args:
        config: 非 Secret 配置；缺项生成受阻记录，不初始化产品。
        steps: 要执行的阶段，None 表示最终完整模式。
        resume: 复用同身份成功记录；失败记录不会自动变绿。
        live: 调用方显式允许真实网络，仍须存在持久授权 campaign。
        backend: 离线测试用实现；生产默认使用现有 Product Runtime。

    Returns:
        全部阶段的安全报告；P11 总发布门由 release 入口进一步聚合。

    """
    values = dict(config or {})
    default_state = (
        Path(str(values["data_dir"])) / "p11-live-state.sqlite3"
        if values.get("data_dir")
        else Path("artifacts/release/p11-live-state.sqlite3")
    )
    state = AcceptanceState(
        Path(str(values.get("state_path", default_state))),
        str(values.get("campaign_id", "unconfigured")),
    )
    if backend is None:
        backend_type = cast(
            Callable[[dict[str, object], AcceptanceState], AcceptanceBackend],
            import_module(
                "rag_app.product.live_acceptance_backend"
            ).ProductAcceptanceBackend,
        )
        backend = backend_type(values, state)
    selected = _selected_steps(steps)
    before = backend.budget_snapshot()
    binding = (
        backend.bind_campaign()
        if values.get("bind_campaign") is True
        else StepResult("NOT_RUN", "CAMPAIGN_BINDING_NOT_REQUESTED")
    )
    records: dict[str, dict[str, object]] = {}
    try:
        for step in STEPS[:-1]:
            identity = canonical_sha256(
                {
                    "self": backend.identity(step),
                    "dependencies": {
                        name: records[name]["identity"]
                        for name in DEPENDENCIES[step]
                    },
                }
            )
            prior = state.latest(step)
            dependencies_pass = _dependencies_ready(step, records)
            reusable = (
                prior is not None
                and prior["identity"] == identity
                and prior["status"] == "PASS"
                and dependencies_pass
                and backend.evidence_is_current(step, prior)
            )
            if step != "config_check" and resume and reusable:
                records[step] = cast(dict[str, object], prior)
                continue
            if step not in selected:
                records[step] = {
                    "status": "NOT_RUN",
                    "reason": "DEPENDENCY_IDENTITY_CHANGED"
                    if prior is not None and not reusable
                    else "STEP_NOT_SELECTED",
                    "evidence": {},
                    "identity": identity,
                    "provenance": "未执行",
                }
                continue
            if step != "config_check" and not live:
                result = StepResult("BLOCKED", "BLOCKED_AUTHORIZATION")
            elif step != "config_check" and (
                values.get("bind_campaign") is True and binding.status != "PASS"
            ):
                result = StepResult("BLOCKED", binding.reason, binding.evidence)
            elif not dependencies_pass:
                result = StepResult("BLOCKED", "BLOCKED_DEPENDENCY")
            else:
                result = backend.execute(step)
            records[step] = state.record(step, identity, result)
    finally:
        after = backend.budget_snapshot()
        backend.close()
    connectivity = all(
        records[name]["status"] == "PASS"
        for name in (
            "aliyun_document_canary",
            "aliyun_query_canary",
            "jina_connection",
        )
    )
    complete = all(item["status"] == "PASS" for item in records.values())
    final_result = StepResult(
        "PASS" if complete else "BLOCKED",
        "ALL_LIVE_STEPS_PASSED" if complete else "REQUIRED_STEPS_INCOMPLETE",
        {"required_steps": list(STEPS[:-1])},
    )
    records["final_report"] = state.record(
        "final_report", canonical_sha256(records), final_result
    )
    requested = (
        set(STEPS[:-1])
        if steps is None
        else {part.strip() for item in steps for part in item.split(",")}
    )
    selected_statuses = {str(records[name]["status"]) for name in requested}
    if values.get("bind_campaign") is True:
        selected_statuses.add(binding.status)
    selected_status = (
        "FAIL"
        if "FAIL" in selected_statuses
        else "BLOCKED"
        if "BLOCKED" in selected_statuses
        else "NOT_RUN"
        if "NOT_RUN" in selected_statuses
        else "PASS"
    )
    return {
        "schema": "p11-live-acceptance-v1",
        "campaign_id": values.get("campaign_id"),
        "steps": records,
        "budget": _budget_report(before, after),
        "campaign_binding": {
            "status": binding.status,
            "reason": binding.reason,
            "evidence": binding.evidence,
        },
        "selected_steps": sorted(requested),
        "selected_steps_status": selected_status,
        "overall_release_status": "BLOCKED",
        "CONNECTIVITY_READY": connectivity,
        "QUALITY_READY": records["citation_quality"]["status"],
        "LIVE_ACCEPTANCE_READY": complete,
        "P11_READY": False,
        "P11_READY_reason": "REQUIRES_RELEASE_GATE_AGGREGATION",
        "MERGE_TO_MAIN_AUTHORIZED": False,
        "private_documents_sent": False,
    }


def _budget_report(
    before: dict[str, object], after: dict[str, object]
) -> dict[str, object]:
    if after.get("status") != "PASS":
        return after
    initial = cast(list[dict[str, object]], before.get("attempts", []))
    final = cast(list[dict[str, object]], after.get("attempts", []))
    initial_ids = {item["attempt_id"] for item in initial}
    delta = [item for item in final if item["attempt_id"] not in initial_ids]
    imported = [item for item in delta if item.get("source_identity")]
    delta = [item for item in delta if not item.get("source_identity")]
    return {
        "status": "PASS",
        "cumulative": after["summary"],
        "this_run": summarize_attempts(delta),
        "imported_history": summarize_attempts(imported),
        "attempt_ids": [item["attempt_id"] for item in delta],
        "providers_this_run": {
            provider: summarize_attempts(
                [item for item in delta if item["provider"] == provider]
            )
            for provider in sorted({str(item["provider"]) for item in delta})
        },
    }
