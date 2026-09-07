"""预算入口绑定真实草稿策略及只读边界的回归。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from rag_app.composition import product_runtime
from rag_app.product.crypto import SecretCipher
from scripts import release
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)


@pytest.fixture
def draft_context(
    tmp_path: Path,
) -> Iterator[tuple[ProductHarness, dict[str, Any]]]:
    """创建无 Provider 验证、无激活和无索引的合法产品草稿上下文。"""
    harness = build_product_harness(tmp_path)
    _, knowledge_base = create_project_and_knowledge_base(harness)
    _, _, jina, aliyun = create_provider_connections(harness)
    # 预算读取要求静止元数据；本夹具不提交作业，先停止后台 SQLite 轮询。
    harness.runtime.jobs.close()
    yield (
        harness,
        {
            "data_dir": str(tmp_path / "data"),
            "knowledge_base_id": knowledge_base,
            "jina_connection_id": jina,
            "aliyun_connection_id": aliyun,
        },
    )
    harness.close()


def _draft(
    context: tuple[ProductHarness, dict[str, Any]],
    *,
    instruct: str = "检索相关文档",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    harness, config = context
    response = harness.client.post(
        f"/api/v1/knowledge-bases/{config['knowledge_base_id']}/retrieval-profiles",
        headers=harness.write_headers,
        json={
            "primary_connection_id": config["jina_connection_id"],
            "primary_embedding_model": "jina-embeddings-v5-text-small",
            "primary_dimension": 1024,
            "primary_document_policy": {"task": "retrieval.passage"},
            "primary_query_policy": {"task": "retrieval.query"},
            "standby_connection_id": config["aliyun_connection_id"],
            "standby_embedding_model": "qwen3.7-text-embedding",
            "standby_dimension": 1024,
            "standby_document_policy": {"text_type": "document"},
            "standby_query_policy": {
                "text_type": "query",
                "query_instruct": instruct,
            },
            "reranker_connection_id": config["jina_connection_id"],
            "reranker_model": "jina-reranker-v3.5",
            "failover_enabled": True,
            "standby_budget": {"requests": 2, "tokens": 4096},
            "retrieval_policy": policy or {"rrf_k": 60},
        },
    )
    response.raise_for_status()
    return {
        **config,
        "source_profile_revision_id": response.json()["profile_revision_id"],
    }


def _command(tmp_path: Path, config: dict[str, Any]) -> tuple[list[str], Path]:
    source = tmp_path / "budget-history.json"
    source.write_text(
        json.dumps(
            {
                "reserved": 6,
                "estimated_input_tokens": 157,
                "request_limit": 25,
                "estimated_token_limit": 1000,
                "providers": {
                    "jina": {"reserved": 4, "estimated_input_tokens": 119},
                    "aliyun": {"reserved": 2, "estimated_input_tokens": 38},
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "acceptance.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "plan.json"
    return [
        "budget-plan",
        "--config",
        str(config_path),
        "--budget-history",
        str(source),
        "--plan-output",
        str(output),
    ], output


def _plan(tmp_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    command, output = _command(tmp_path, config)
    assert release.main(command) == 0
    return cast(dict[str, Any], json.loads(output.read_text(encoding="utf-8")))


def test_actual_draft_instruct_changes_identity_and_estimate(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
) -> None:
    first = _plan(tmp_path, _draft(draft_context))
    second = _plan(
        tmp_path,
        _draft(draft_context, instruct="请根据查询检索相关公开文档。" * 30),
    )
    assert first["actual_profile_bound"] is True
    assert first["resolved_profile"]["profile_status"] == "draft"
    assert first["budget_policy_identity"] != second["budget_policy_identity"]
    assert first["query_instruct_identity"] != second["query_instruct_identity"]
    assert first["totals_additional_work"] != second["totals_additional_work"]
    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert first["sample_count"] == second["sample_count"] == 30
    assert first["lanes"] == second["lanes"] == ["primary", "standby"]
    assert first["quality_thresholds_changed"] is False


@pytest.mark.parametrize(
    "policy",
    [
        {"rerank_candidate_limit": 1},
        {"rerank_text_char_limit": 8},
    ],
)
def test_actual_rerank_policy_changes_rerank_estimate(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
    policy: dict[str, Any],
) -> None:
    baseline = _plan(tmp_path, _draft(draft_context))
    limited = _plan(tmp_path, _draft(draft_context, policy=policy))

    def tokens(plan: dict[str, Any]) -> int:
        return sum(
            row["estimated_tokens"]
            for row in plan["operations"]
            if "rerank" in row["operation"]
        )

    assert tokens(limited) != tokens(baseline)


def test_display_name_does_not_change_budget_semantics(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _draft(draft_context)
    before = _plan(tmp_path, config)
    metadata = release._budget_profile_metadata(
        argparse.Namespace(container=None), config
    )
    for connection in cast(list[dict[str, Any]], metadata["connections"]):
        connection["display_name"] = "修改显示名"
    monkeypatch.setattr(
        release, "_budget_profile_metadata", lambda *_: metadata
    )
    after = _plan(tmp_path, config)
    assert before["budget_policy_identity"] == after["budget_policy_identity"]
    assert before["operations"] == after["operations"]


def test_missing_profile_marks_unapproved_default_assumptions(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, {})
    assert plan["actual_profile_bound"] is False
    assert plan["status"] == "PROPOSED"
    assert plan["activated"] is False
    assert plan["approver"] is None
    assert "未提供 Profile" in plan["configuration_assumption"]


@pytest.mark.parametrize("fault", ["missing_profile", "wrong_connection"])
def test_invalid_profile_does_not_fall_back(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
    fault: str,
) -> None:
    config = _draft(draft_context)
    key = (
        "source_profile_revision_id"
        if fault == "missing_profile"
        else "jina_connection_id"
    )
    config[key] = "does-not-exist"
    command, output = _command(tmp_path, config)
    assert release.main(command) == 2
    assert not output.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("failover_enabled", 0),
        ("standby_embedding_model", "unknown-model"),
    ],
)
def test_invalid_topology_or_model_cannot_produce_default_plan(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    config = _draft(draft_context)
    metadata = release._budget_profile_metadata(
        argparse.Namespace(container=None),
        config,
    )
    cast(dict[str, Any], metadata["profile"])[field] = value
    monkeypatch.setattr(
        release, "_budget_profile_metadata", lambda *_: metadata
    )
    command, output = _command(tmp_path, config)
    assert release.main(command) == 2
    assert not output.exists()


def test_plan_preserves_database_ledger_and_never_decrypts_or_calls_http(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _draft(draft_context)
    command, output = _command(tmp_path, config)

    def snapshot() -> dict[str, bytes]:
        return {
            str(path): path.read_bytes()
            for path in (tmp_path / "data").rglob("*")
            if path.is_file()
        }

    before = snapshot()
    history = (tmp_path / "budget-history.json").read_bytes()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("预算计划禁止解密、构造 Runtime 或请求 Provider")

    monkeypatch.setattr(SecretCipher, "decrypt", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(product_runtime, "build_product_runtime", forbidden)
    monkeypatch.setattr(release, "_run_live_acceptance", forbidden)
    assert release.main(command) == 0
    assert snapshot() == before
    assert (tmp_path / "budget-history.json").read_bytes() == history
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["current_authorization"]["used_requests"] == 6
    assert plan["current_authorization"]["used_estimated_tokens"] == 157
    assert plan["new_provider_http"] == 0


def test_local_writable_wal_is_blocked_without_changing_source_files(
    draft_context: tuple[ProductHarness, dict[str, Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """新草稿仅提交在 WAL 时必须明确阻塞，不能静默读旧主库。"""
    data = tmp_path / "data"
    db = sqlite3.connect(data / "universal-rag.sqlite3")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        config = _draft(draft_context, instruct="读取已提交 WAL 中的方案")
        assert (data / "universal-rag.sqlite3-wal").stat().st_size > 0
        before = {
            path.name: path.read_bytes()
            for path in data.iterdir()
            if path.is_file()
        }
        command, output = _command(tmp_path, config)
        assert release.main(command) != 0
        assert "BUDGET_READONLY_WAL" in capsys.readouterr().err
        assert not output.exists()
        assert {
            path.name: path.read_bytes()
            for path in data.iterdir()
            if path.is_file()
        } == before
    finally:
        db.close()


def test_container_data_path_requires_container_without_creating_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Path, "mkdir", lambda *_args, **_kwargs: pytest.fail("不得新建目录")
    )
    config = {"data_dir": "/data", "source_profile_revision_id": "profile"}
    command, output = _command(tmp_path, config)
    assert release.main(command) == 2
    assert not output.exists()


def test_container_metadata_mount_is_read_only_and_never_stops_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "_required_executable", lambda _: "docker")
    monkeypatch.setattr(
        release,
        "_campaign_container_metadata",
        lambda *_: {
            "image": "sha256:synthetic",
            "mounts": [
                {
                    "Destination": "/data",
                    "Type": "volume",
                    "Name": "existing-data",
                },
                {
                    "Destination": "/run/rag-secrets",
                    "Type": "volume",
                    "Name": "secret-volume",
                },
            ],
        },
    )

    def capture(
        command: Sequence[str], *, input_text: str | None = None
    ) -> str:
        commands.append(tuple(command))
        if input_text is None:
            assert command[:3] == ("docker", "image", "inspect")
            return "sha256:reader"
        query = json.loads(input_text)["connection_query"].lower()
        assert " from credentials" not in query
        assert "encrypted" not in query
        return '{"profile": null, "connections": []}'

    monkeypatch.setattr(release, "_capture", capture)
    release._budget_profile_metadata(
        argparse.Namespace(container="existing-app"),
        {
            "data_dir": "/data",
            "source_profile_revision_id": "profile",
        },
    )
    assert len(commands) == 2
    command = commands[1]
    assert command[:3] == ("docker", "run", "--rm")
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "type=volume,src=existing-data,dst=/data,readonly" in command
    assert "secret-volume" not in " ".join(command)
    assert "stop" not in command
