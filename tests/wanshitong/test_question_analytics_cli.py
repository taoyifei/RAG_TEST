"""F05 单次 CLI 复用部署配置并给出可监控退出码。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.wanshitong.models import ScopeBinding
from rag_app.wanshitong.question_analytics import QuestionAnalyticsService
from scripts import wanshitong_question_analytics as cli
from tests.wanshitong.support import PublicHarness, synthetic_answer


def _configure_environment(
    harness: PublicHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把合成 Runtime 暴露为与候选部署相同的环境变量形状。"""
    settings = harness.product.runtime.settings
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    monkeypatch.setenv("RAG_ROOT_PATH", "/kb")
    monkeypatch.setenv("RAG_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv(
        "RAG_ADMIN_BOOTSTRAP_TOKEN_FILE", str(settings.bootstrap_token_file)
    )
    assert settings.master_key_file is not None
    monkeypatch.setenv("RAG_MASTER_KEY_FILE", str(settings.master_key_file))
    monkeypatch.setenv("RAG_HISTORY_SAVE_BODY", "true")
    monkeypatch.setenv("RAG_HISTORY_RETENTION_DAYS", "7")
    monkeypatch.setenv("RAG_WANSHITONG_AUTH_MODE", "sso")
    monkeypatch.setenv("RAG_WANSHITONG_SSO_DEPLOYMENT_ID", "candidate_8289")
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_ENTRIES",
        json.dumps(
            [
                {
                    "id": "candidate",
                    "origin": "http://127.0.0.1:8289",
                    "authorize_url": "http://127.0.0.1/sso/authorize",
                }
            ]
        ),
    )
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_VALIDATE_URL", "http://127.0.0.1/sso/validate"
    )
    monkeypatch.setenv("RAG_WANSHITONG_SSO_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv(
        "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE",
        str(settings.bootstrap_token_file),
    )


def _add_history(harness: PublicHarness, *, deployment_id: str) -> None:
    """只写合成问答历史，不执行检索或 Provider。"""
    binding = harness.scope_service.binding()
    scope = KnowledgeBaseScope(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
    )
    trace_id = (
        "trace_" + ("a" if deployment_id == "candidate_8289" else "b") * 32
    )
    history = harness.product.runtime.history
    history.start(
        trace_id,
        scope,
        "如何办理？",
        owner_id="rdms:1001",
        save_body=True,
        audit_context=QueryAuditContext(
            trace_id=trace_id,
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            owner_id="rdms:1001",
            deployment_id=deployment_id,
            identity_source="RDMS_SSO",
            traffic_class="INTERACTIVE",
            classification_source="PUBLIC_ENDPOINT",
            entrypoint="manual",
        ),
    )
    history.finish(
        trace_id,
        result=synthetic_answer(trace_id, include_evidence=False),
        error=None,
        cancelled=False,
    )


def test_cli_uses_existing_scope_key_and_deployment(
    public_harness: PublicHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """候选刷新只纳入该部署历史，不创建模型或另一知识范围。"""
    _configure_environment(public_harness, monkeypatch)
    _add_history(public_harness, deployment_id="candidate_8289")
    _add_history(public_harness, deployment_id="other_deployment")

    result = cli.main(["--timeout-seconds", "10"])

    payload = json.loads(capsys.readouterr().out)
    assert result == cli.EXIT_COMPLETE
    assert payload["state"] == "COMPLETE"
    assert payload["deployment_id"] == "candidate_8289"
    assert payload["source_total"] == 1
    assert payload["eligible_count"] == 1
    assert "question" not in payload
    assert "key" not in payload
    assert public_harness.scope_service.binding().knowledge_base_id


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("COMPLETE", cli.EXIT_COMPLETE),
        ("LIMITED", cli.EXIT_LIMITED),
        ("FAILED", cli.EXIT_FAILED),
    ],
)
def test_cli_terminal_exit_codes(
    state: str,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """定时入口可直接用状态码区分完整、超限与失败。"""
    binding = ScopeBinding(
        project_id="prj_" + "1" * 32,
        knowledge_base_id="kb_" + "2" * 32,
        created_at="2026-09-23T00:00:00Z",
    )
    service = Mock(spec=QuestionAnalyticsService)
    service.refresh.return_value = {"run_id": "qrun_test"}
    service.run.return_value = {
        "run_id": "qrun_test",
        "state": state,
        "failure_code": "TEST_LIMIT" if state == "LIMITED" else None,
    }
    monkeypatch.setattr(cli, "_build_service", lambda: (service, binding))

    assert cli.main([]) == expected
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == state
    service.refresh.assert_called_once_with(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
    )


def test_cli_timeout_is_bounded_and_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """长时间 BUILDING 返回明确超时码和当前 run ID。"""
    binding = ScopeBinding(
        project_id="prj_" + "1" * 32,
        knowledge_base_id="kb_" + "2" * 32,
        created_at="2026-09-23T00:00:00Z",
    )
    service = Mock(spec=QuestionAnalyticsService)
    service.refresh.return_value = {"run_id": "qrun_test"}
    service.run.return_value = {"run_id": "qrun_test", "state": "BUILDING"}
    times = iter((0.0, 2.0))
    monkeypatch.setattr(cli, "_build_service", lambda: (service, binding))
    monkeypatch.setattr(cli, "monotonic", lambda: next(times))

    assert cli.main(["--timeout-seconds", "1"]) == cli.EXIT_TIMEOUT
    assert json.loads(capsys.readouterr().out) == {
        "deployment_id": None,
        "eligible_count": None,
        "failure_code": None,
        "finished_at": None,
        "run_id": "qrun_test",
        "source_total": None,
        "state": "TIMEOUT",
    }


def test_cli_missing_database_does_not_create_one(
    public_harness: PublicHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """环境指向不存在的数据库时先失败，避免生成空库。"""
    _configure_environment(public_harness, monkeypatch)
    missing = tmp_path / "missing-data"
    monkeypatch.setenv("RAG_DATA_DIR", str(missing))

    assert cli.main([]) == cli.EXIT_ERROR
    assert not missing.exists()
    assert json.loads(capsys.readouterr().out)["state"] == "ERROR"
