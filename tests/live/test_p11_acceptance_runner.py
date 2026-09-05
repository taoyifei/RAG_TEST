"""阶段续跑的离线回归，不调用供应商或读取用户凭据。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from rag_app.product.live_acceptance import (
    STEPS,
    AcceptanceState,
    StepResult,
    run_acceptance,
)


@dataclass
class FakeBackend:
    """只验证调度语义的替身，不能产生真实发布就绪标记。"""

    failures: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    closed: bool = False
    expired: set[str] = field(default_factory=set)
    configuration: dict[str, object] = field(default_factory=dict)

    def identity(self, step: str) -> str:
        """返回离线配置版本。"""
        return step + self.revisions.get(step, ":v1")

    def execute(self, step: str) -> StepResult:
        """保存实际调用序列。"""
        self.calls.append(step)
        status = self.failures.get(step, "PASS")
        return StepResult(
            status,
            "OFFLINE_SCHEDULER_FIXTURE",
            self.configuration if step == "config_check" else {},
        )

    def evidence_is_current(
        self, step: str, record: Mapping[str, object]
    ) -> bool:
        """调度测试独立控制证据有效期，不冒充 Live 验证。"""
        return record["status"] == "PASS" and step not in self.expired

    def close(self) -> None:
        """确认资源清理回调被执行。"""
        self.closed = True

    def budget_snapshot(self) -> dict[str, object]:
        """替身不冒充真实账本。"""
        return {"status": "NOT_RUN", "reason": "OFFLINE_FIXTURE"}

    def bind_campaign(self) -> StepResult:
        """替身不写入真实授权。"""
        return StepResult("NOT_RUN", "OFFLINE_FIXTURE")


def _config(path: Path) -> dict[str, object]:
    return {"state_path": str(path / "state.sqlite3"), "campaign_id": "test"}


def _steps(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], report["steps"])


def test_one_canary_does_not_require_twenty_three_requests(tmp_path: Path):
    backend = FakeBackend()
    report = run_acceptance(
        _config(tmp_path),
        steps=("aliyun_document_canary",),
        live=True,
        backend=backend,
    )
    assert backend.calls == ["config_check", "aliyun_document_canary"]
    assert _steps(report)["aliyun_document_canary"]["status"] == "PASS"
    assert _steps(report)["aliyun_query_canary"]["status"] == "NOT_RUN"
    assert report["LIVE_ACCEPTANCE_READY"] is False
    assert report["P11_READY"] is False


def test_failed_document_blocks_query_but_does_not_block_jina(tmp_path: Path):
    backend = FakeBackend(failures={"aliyun_document_canary": "FAIL"})
    report = run_acceptance(_config(tmp_path), live=True, backend=backend)
    assert "aliyun_query_canary" not in backend.calls
    assert "jina_connection" in backend.calls
    assert "dual_index" not in backend.calls
    assert (
        _steps(report)["aliyun_query_canary"]["reason"] == "BLOCKED_DEPENDENCY"
    )
    assert backend.closed


def test_resume_skips_current_success_and_executes_next_group(tmp_path: Path):
    first = FakeBackend()
    run_acceptance(
        _config(tmp_path),
        steps=("aliyun_document_canary",),
        live=True,
        backend=first,
    )
    second = FakeBackend()
    report = run_acceptance(
        _config(tmp_path),
        steps=("aliyun_query_canary",),
        live=True,
        backend=second,
    )
    assert second.calls == ["config_check", "aliyun_query_canary"]
    assert _steps(report)["aliyun_document_canary"]["provenance"] == "有效复用"


def test_changed_query_policy_invalidates_downstream_only(tmp_path: Path):
    run_acceptance(_config(tmp_path), live=True, backend=FakeBackend())
    backend = FakeBackend(revisions={"aliyun_query_canary": ":v2"})
    report = run_acceptance(
        _config(tmp_path), steps=("primary_query",), live=True, backend=backend
    )
    assert _steps(report)["jina_connection"]["status"] == "PASS"
    assert _steps(report)["aliyun_document_canary"]["status"] == "PASS"
    assert _steps(report)["aliyun_query_canary"]["status"] == "NOT_RUN"
    assert _steps(report)["dual_index"]["status"] == "NOT_RUN"
    assert _steps(report)["primary_query"]["status"] == "BLOCKED"
    assert backend.calls == ["config_check"]


def test_no_live_never_executes_online_backend(tmp_path: Path):
    backend = FakeBackend()
    report = run_acceptance(_config(tmp_path), backend=backend)
    assert backend.calls == ["config_check"]
    assert (
        _steps(report)["aliyun_document_canary"]["reason"]
        == "BLOCKED_AUTHORIZATION"
    )
    assert report["CONNECTIVITY_READY"] is False


def test_missing_config_writes_blocked_report_without_runtime(tmp_path: Path):
    report = run_acceptance({"state_path": str(tmp_path / "state.db")})
    assert _steps(report)["config_check"]["status"] == "BLOCKED"
    assert "MISSING_CONFIG" in str(_steps(report)["config_check"]["reason"])
    assert set(_steps(report)) == set(STEPS)


def test_campaign_switch_does_not_reuse_prior_success(tmp_path: Path):
    run_acceptance(_config(tmp_path), live=True, backend=FakeBackend())
    backend = FakeBackend()
    report = run_acceptance(
        {**_config(tmp_path), "campaign_id": "new-authorization"},
        steps=("primary_query",),
        live=True,
        backend=backend,
    )
    assert _steps(report)["dual_index"]["status"] == "NOT_RUN"
    assert _steps(report)["primary_query"]["status"] == "BLOCKED"


def test_state_preserves_failed_history_and_resume_resources(tmp_path: Path):
    path = tmp_path / "state.db"
    first = AcceptanceState(path, "test")
    first.record(
        "aliyun_document_canary", "identity", StepResult("FAIL", "HTTP_403")
    )
    first.resource("index_job_id", "job-synthetic")
    second = AcceptanceState(path, "test")
    assert second.resource("index_job_id") == "job-synthetic"
    assert second.latest("aliyun_document_canary")["reason"] == "HTTP_403"
    assert (
        AcceptanceState(path, "other").latest("aliyun_document_canary") is None
    )


def test_unknown_steps_are_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="未知 Live 阶段"):
        run_acceptance(
            _config(tmp_path), steps=("fictional",), backend=FakeBackend()
        )


def test_expired_success_cannot_be_reused_or_satisfy_dependencies(
    tmp_path: Path,
):
    run_acceptance(_config(tmp_path), live=True, backend=FakeBackend())
    backend = FakeBackend(expired={"aliyun_document_canary"})
    report = run_acceptance(
        _config(tmp_path),
        steps=("aliyun_query_canary",),
        live=True,
        backend=backend,
    )
    assert _steps(report)["aliyun_document_canary"]["status"] == "NOT_RUN"
    assert _steps(report)["aliyun_query_canary"]["status"] == "BLOCKED"
    assert backend.calls == ["config_check"]


def test_selected_success_is_separate_from_incomplete_release(tmp_path: Path):
    report = run_acceptance(
        _config(tmp_path), steps=("config_check",), backend=FakeBackend()
    )
    assert report["selected_steps_status"] == "PASS"
    assert report["overall_release_status"] == "BLOCKED"
    assert report["P11_READY"] is False


def test_blocked_aliyun_configuration_preserves_current_jina_evidence(
    tmp_path: Path,
):
    run_acceptance(_config(tmp_path), live=True, backend=FakeBackend())
    backend = FakeBackend(
        failures={"config_check": "BLOCKED"},
        configuration={
            "campaign_binding": "PASS",
            "connections": {
                "jina_connection_id": {"status": "PASS"},
                "aliyun_connection_id": {"status": "BLOCKED"},
            },
        },
    )
    report = run_acceptance(
        _config(tmp_path),
        steps=("jina_connection",),
        live=True,
        backend=backend,
    )
    assert _steps(report)["config_check"]["status"] == "BLOCKED"
    assert _steps(report)["jina_connection"]["status"] == "PASS"
    assert _steps(report)["jina_connection"]["provenance"] == "有效复用"
    assert report["selected_steps_status"] == "PASS"
    assert report["P11_READY"] is False
    assert backend.calls == ["config_check"]
