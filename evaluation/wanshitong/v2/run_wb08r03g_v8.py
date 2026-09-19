"""在8289以唯一版本包运行V8门禁，所有正文留在仓库外私有目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from evaluation.wanshitong.v2 import run_wb08r03f_candidate as legacy
from evaluation.wanshitong.v2.wb08r03g_v8_metrics import (
    packet_observation,
    performance_observation,
)

_REPO = Path(__file__).resolve().parents[3]
_CANDIDATE_CONTAINER = "wanshitong-wb08r01-app"
_GATES = (
    "preflight-16",
    "evidence-pack-12",
    "core-answer-16",
    "failed-24",
    "natural-36",
    "full-96",
    "concurrency-4",
)
_REPEATED = ("WB08R-N-031", "WB08R-N-033", "WB08R-F-015", "WB08R-F-013")
_CONTROLS = ("WB08R-F-024", "WB08R-F-034", "WB08R-N-035", "WB08R-A-001")
_VERSION_FIELDS = (
    "code_sha",
    "runtime_tree_sha256",
    "image_id",
    "base_digest",
    "dependency_lock_sha256",
    "asset_manifest_sha256",
    "configuration_sha256",
    "active_index_revision_id",
    "index_fingerprint",
    "serving_fingerprint",
    "schema_revision",
    "evidence_identity_revision",
    "packet_revision",
    "dataset_revision",
    "dataset_sha256",
    "runner_sha256",
    "runtime_guard_sha256",
)

_DATABASE_GUARD = """
import hashlib, json, sqlite3
from rag_app._build_revision import SOURCE_REVISION
db = sqlite3.connect('file:/data/universal-rag.sqlite3?mode=ro', uri=True)
rows = {}
for table in ('knowledge_bases', 'retrieval_profile_revisions',
              'knowledge_base_model_settings', 'index_revisions',
              'provider_connections'):
    query = 'SELECT * FROM ' + table + ' ORDER BY 1'
    rows[table] = db.execute(query).fetchall()
payload = json.dumps(rows, sort_keys=True, default=str, separators=(',', ':'))
digest = hashlib.sha256(payload.encode()).hexdigest()
print(json.dumps({'code_sha': SOURCE_REVISION,
                  'database_behavior_sha256': digest}))
"""


def observe_runtime_guard() -> dict[str, Any]:
    """只读绑定候选容器与可变配置，数据库正文和秘密只进入摘要。

    Args:
        无参数；读取固定的8289候选容器。

    Returns:
        容器、代码、配置和索引状态的安全摘要。

    """
    inspected = subprocess.run(  # noqa: S603 - 固定候选容器的只读身份。
        ["/usr/bin/docker", "inspect", _CANDIDATE_CONTAINER],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    container = json.loads(inspected.stdout)[0]
    ports = container["NetworkSettings"]["Ports"]
    if ports.get("8088/tcp") != [{"HostIp": "127.0.0.1", "HostPort": "8289"}]:
        raise ValueError("CANDIDATE_PORT_IDENTITY_CHANGED")
    environment = container["Config"]["Env"]
    if any(
        entry.startswith("RAG_PRIVATE_REPLAY_") and entry.partition("=")[2]
        for entry in environment
    ):
        raise ValueError("PRIVATE_CAPTURE_MUST_BE_DISABLED_FOR_QUALITY_GATES")
    captured = subprocess.run(  # noqa: S603 - 固定候选容器与只读脚本。
        [
            "/usr/bin/docker",
            "exec",
            _CANDIDATE_CONTAINER,
            "python",
            "-c",
            _DATABASE_GUARD,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    observed = json.loads(captured.stdout)
    identity = {
        "container_id": container["Id"],
        "image_id": container["Image"],
        "code_sha": observed["code_sha"],
        "database_behavior_sha256": observed["database_behavior_sha256"],
        "environment_sha256": hashlib.sha256(
            json.dumps(sorted(environment), separators=(",", ":")).encode()
        ).hexdigest(),
    }
    identity["runtime_guard_sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return identity


def verify_runtime(bundle: dict[str, Any]) -> None:
    """任何容器、配置、活动索引变化都终止本批，不能混用旧成绩。

    Args:
        bundle: 请求必须匹配的冻结版本包。

    Returns:
        身份一致时无返回值，漂移时抛出错误。

    """
    observed = observe_runtime_guard()
    if any(
        observed[key] != bundle[key]
        for key in ("runtime_guard_sha256", "code_sha", "image_id")
    ):
        raise ValueError("SERVING_VERSION_CHANGED")


def bind_version(bundle: dict[str, Any]) -> dict[str, Any]:
    """版本身份不完整时在请求前失败，禁止跨候选拼接成绩。

    Args:
        bundle: 含源码、镜像、配置、索引和评测身份的安全记录。

    Returns:
        附加统一版本摘要后的记录。

    """
    if bundle.get("container") != _CANDIDATE_CONTAINER:
        raise ValueError("ONLY_8289_CANDIDATE_CONTAINER_ALLOWED")
    if any(
        not isinstance(bundle.get(key), str)
        or not bundle[key]
        or bundle[key] in {"NOT_OBSERVED", "UNKNOWN"}
        for key in _VERSION_FIELDS
    ):
        raise ValueError("VERSION_IDENTITY_INCOMPLETE")
    normalized = {key: bundle[key] for key in _VERSION_FIELDS}
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**bundle, "version_bundle_sha256": digest}


def _private_output(directory: Path) -> Path:
    resolved = directory.resolve()
    if resolved == _REPO or _REPO in resolved.parents:
        raise ValueError("PRIVATE_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")
    if directory.exists():
        raise ValueError("FRESH_GATE_DIRECTORY_REQUIRED")
    directory.mkdir(mode=0o700, parents=True)
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("PRIVATE_DIRECTORY_PERMISSIONS")
    return directory


def _write(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _enrich(
    rows: list[dict[str, Any]], bundle: dict[str, Any]
) -> list[dict[str, Any]]:
    for row in rows:
        events, error = legacy.read_trace(
            row.get("trace_id"),
            trace_db=None,
            trace_container=_CANDIDATE_CONTAINER,
        )
        row["version_bundle_sha256"] = bundle["version_bundle_sha256"]
        row["prepared_sent"] = packet_observation(events)
        row["v8_trace_error"] = error
    return rows


def _run_legacy(
    directory: Path,
    gate: str,
    bundle: dict[str, Any],
    case_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    previous = legacy._RESULTS
    original_chat = legacy._chat

    def _bound_chat(
        opener: urllib.request.OpenerDirector,
        csrf: str,
        base_url: str,
        conversation_id: str,
        question: str,
    ) -> dict[str, Any]:
        verify_runtime(bundle)
        result = original_chat(
            opener, csrf, base_url, conversation_id, question
        )
        verify_runtime(bundle)
        return result

    legacy._RESULTS = directory
    legacy._chat = _bound_chat
    try:
        summary = legacy.run(
            gate,
            directory / "observations.ndjson",
            directory / "answers.private.ndjson",
            base_url="http://127.0.0.1:8289",
            trace_db=None,
            trace_container=_CANDIDATE_CONTAINER,
            case_ids=frozenset({case_id}) if case_id else None,
        )
    finally:
        legacy._RESULTS = previous
        legacy._chat = original_chat
    return _records(directory / "observations.ndjson"), summary


def run(gate: str, directory: Path, bundle: dict[str, Any]) -> dict[str, Any]:
    """复用真实A～F定义并隔离每个版本、每次运行的私有结果。

    Args:
        gate: 冻结门禁名称。
        directory: 尚不存在的仓库外受控目录。
        bundle: 当前部署的完整版本记录。

    Returns:
        含发送包观测及待完成语义评审状态的门禁摘要。

    """
    version = bind_version(bundle)
    if gate not in _GATES:
        raise ValueError("UNKNOWN_GATE")
    verify_runtime(version)
    directory = _private_output(directory)
    _write(directory / "version.safe.json", version)
    if gate == "preflight-16":
        runs = tuple(
            (f"repeat-{repeat + 1}-{case_id}", case_id)
            for case_id in _REPEATED
            for repeat in range(3)
        ) + tuple((f"control-{case_id}", case_id) for case_id in _CONTROLS)
        rows: list[dict[str, Any]] = []
        for name, case_id in runs:
            trial = directory / name
            trial.mkdir(mode=0o700)
            observed, _ = _run_legacy(trial, "natural-36", version, case_id)
            for row in observed:
                row["preflight_run_id"] = name
                row["run_id"] = name
            rows.extend(observed)
        summary = legacy.summarize(rows)
        summary["preflight_gate"] = "NOT_REVIEWED"
        summary["expected_request_count"] = 16
    else:
        rows, summary = _run_legacy(directory, gate, version)
    rows = _enrich(rows, version)
    unknown = sum(
        row["prepared_sent"]["prepared_sent_status"] == "NOT_OBSERVED"
        or row["prepared_sent"]["cache_hit"] == "NOT_OBSERVED"
        for row in rows
    )
    failures = sum(
        row["prepared_sent"]["prepared_sent_status"] == "FAILED" for row in rows
    )
    cache_hits = sum(row["prepared_sent"]["cache_hit"] is True for row in rows)
    summary["v8_packet_status"] = (
        "FAILED"
        if failures or cache_hits
        else "NOT_OBSERVED"
        if unknown
        else "OBSERVED"
    )
    summary["cache_hit_count"] = cache_hits
    summary["version_bundle_sha256"] = version["version_bundle_sha256"]
    summary["semantic_review_required"] = True
    summary["performance_by_path"] = performance_observation(rows)
    _write(directory / "observations-v8.safe.json", rows)
    _write(directory / "summary-v8.safe.json", summary)
    print(
        json.dumps(
            {
                "gate": gate,
                "case_count": len(rows),
                "v8_packet_status": summary["v8_packet_status"],
            }
        )
    )
    return summary


def review(  # noqa: PLR0912, PLR0915 - 保留各 Gate 的独立验收定义。
    directory: Path, gate: str, judgments_path: Path
) -> dict[str, Any]:
    """只评审已有同版本结果；逐答案事实判断缺失时不能宣告通过。

    Args:
        directory: 本次门禁的仓库外受控目录。
        gate: 与已有结果一致的门禁名称。
        judgments_path: 绑定每次问题及答案摘要的事实评审文件。

    Returns:
        合并既有门槛、实际发送和事实覆盖的最终验收摘要。

    """
    rows = json.loads((directory / "observations-v8.safe.json").read_text())
    summary: dict[str, Any] = json.loads(
        (directory / "summary-v8.safe.json").read_text()
    )
    version = bind_version(
        json.loads((directory / "version.safe.json").read_text())
    )
    if any(
        row.get("version_bundle_sha256") != version["version_bundle_sha256"]
        for row in rows
    ):
        raise ValueError("MIXED_VERSION_RESULTS")
    previous = legacy._RESULTS
    legacy._RESULTS = directory
    try:
        judgments = legacy._load_judgments(judgments_path)
    finally:
        legacy._RESULTS = previous
    if gate == "preflight-16":
        checks = [
            legacy._functional_gate(
                [row], frozenset({row["case_id"]}), judgments
            )
            for row in rows
        ]
        expected = {
            **dict.fromkeys(_REPEATED, 3),
            **dict.fromkeys(_CONTROLS, 1),
        }
        observed = {
            case_id: sum(row["case_id"] == case_id for row in rows)
            for case_id in {row["case_id"] for row in rows}
        }
        legacy_status = (
            "FAILED"
            if observed != expected
            or any(c["status"] == "FAILED" for c in checks)
            else "NOT_REVIEWED"
            if any(c["status"] != "PASSED" for c in checks)
            else "PASSED"
        )
        summary["preflight_gate"] = {"status": legacy_status, "checks": checks}
    elif gate in {"natural-36", "core-answer-16"}:
        expected_ids = (
            legacy._NATURAL_36
            if gate == "natural-36"
            else frozenset(legacy._CORE_ANSWER_16)
        )
        check = legacy._functional_gate(rows, expected_ids, judgments)
        summary["functional_gate"] = check
        legacy_status = check["status"]
    elif gate == "full-96":
        check = legacy._full_96_gate(rows, legacy.summarize(rows), judgments)
        summary["full_96_gate"] = check
        legacy_status = check["status"]
    elif gate == "failed-24":
        _, truth, _ = legacy.load_truth()
        check = legacy._failed_24_gate(rows, truth, judgments)
        summary["failed_24_gate"] = check
        legacy_status = check["status"]
    elif gate == "evidence-pack-12":
        legacy_status = legacy._evidence_pack_12_gate(rows)
    elif gate == "concurrency-4":
        check = legacy._concurrency_gate(rows)
        summary["concurrency_gate"] = check
        legacy_status = check["status"]
    else:
        raise ValueError("UNKNOWN_GATE")
    fact_failures = []
    for row in rows:
        judgment = judgments.get(row["run_id"])
        if judgment is None:
            fact_failures.append(f"{row['run_id']}:MISSING_FACT_REVIEW")
            continue
        fact_failures.extend(
            f"{row['run_id']}:{field.upper()}"
            for field in legacy._REVIEW_FIELDS
            if judgment[field] != 0
        )
        if (
            judgment["question_sha256"] != row["question_sha256"]
            or judgment["answer_sha256"] != row["answer_sha256"]
        ):
            fact_failures.append(f"{row['run_id']}:STALE_FACT_REVIEW")
        if row["frozen_expected_behavior"] in {"ANSWER", "LIMITED"} and (
            judgment.get("source_locations_verified") is not True
            or judgment.get("requested_fact_coverage")
            not in (
                {"COMPLETE"}
                if row["frozen_expected_behavior"] == "ANSWER"
                else {"COMPLETE", "LIMITED_AS_FROZEN"}
            )
        ):
            fact_failures.append(f"{row['run_id']}:FACT_COVERAGE_NOT_VERIFIED")
    summary["fact_review_failures"] = fact_failures
    summary["v8_gate_status"] = (
        "PASSED"
        if legacy_status == "PASSED"
        and summary["v8_packet_status"] == "OBSERVED"
        and not fact_failures
        else "FAILED"
        if legacy_status == "FAILED" or fact_failures
        else "NOT_REVIEWED"
    )
    _write(directory / "reviewed-summary-v8.safe.json", summary)
    return summary


def main() -> None:
    """仅接受冻结版本文件与新建的仓库外目录。

    Args:
        无参数；从命令行读取运行或评审选项。

    Returns:
        无返回值，结果写入受控目录。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", choices=_GATES, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--version-bundle", type=Path)
    parser.add_argument("--review-judgments", type=Path)
    args = parser.parse_args()
    if args.review_judgments:
        review(args.private_dir, args.gate, args.review_judgments)
    elif args.version_bundle:
        run(
            args.gate,
            args.private_dir,
            json.loads(args.version_bundle.read_text()),
        )
    else:
        parser.error("--version-bundle is required for a new run")


if __name__ == "__main__":
    main()
