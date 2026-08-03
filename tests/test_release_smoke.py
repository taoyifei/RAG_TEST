"""验证本地 smoke release 浅层编排器。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_smoke import (
    SmokeContext,
    SmokeError,
    Stage,
    _execute_smoke,
    _write_handoff,
    _write_report,
    execute_stages,
)


def test_execute_stages_stops_on_first_stable_error() -> None:
    """证明失败后不制造后续噪声并保留首个错误码。"""
    calls: list[str] = []

    def succeed() -> None:
        calls.append("first")

    def fail() -> None:
        calls.append("second")
        raise RuntimeError("raw failure")

    def forbidden() -> None:
        calls.append("third")

    report: dict[str, object] = {"stages": []}
    stages = (
        Stage("first", "FIRST_FAILED", succeed),
        Stage("second", "SECOND_FAILED", fail),
        Stage("third", "THIRD_FAILED", forbidden),
    )

    with pytest.raises(SmokeError) as captured:
        execute_stages(stages, report)

    assert captured.value.code == "SECOND_FAILED"
    assert calls == ["first", "second"]
    assert report["stages"] == [
        {
            "duration_seconds": pytest.approx(0.0, abs=0.1),
            "name": "first",
            "status": "passed",
        },
        {
            "duration_seconds": pytest.approx(0.0, abs=0.1),
            "error_code": "SECOND_FAILED",
            "name": "second",
            "status": "failed",
        },
    ]


def test_release_smoke_is_shallow_and_offline() -> None:
    """锁定编排器只串联既有入口与断网 Docker 命令。"""
    source_path = Path(__file__).parents[1] / "scripts/release_smoke.py"
    source = source_path.read_text(encoding="utf-8")

    assert len(source.splitlines()) <= 640
    for marker in (
        "prepare_runtime_wheels.py",
        "docker buildx build",
        "--network none",
        "asset-selfcheck",
        "freeze_corpus_manifest.py",
        "deployment/package.sh",
        "offline_bundle.py",
        "verify-offline.sh",
        "check_release_safety.py",
        "RELEASE_SAFETY_FAILED",
        "release-smoke-report.json",
        "deployment-handoff.txt",
    ):
        assert marker in source
    for forbidden in ("ssh ", "scp ", "docker push", ".57", ".58", ".60"):
        assert forbidden not in source


def test_release_safety_failure_stops_smoke_without_handoff(
    tmp_path: Path,
) -> None:
    """锁定 payload 安全失败的稳定错误码和无 handoff 语义。"""
    report_path = tmp_path / "artifacts" / "release-smoke-report.json"
    release_dir = tmp_path / "candidate-release"
    release_dir.mkdir()
    context = SmokeContext(
        root=tmp_path,
        report_path=report_path,
        head="a" * 40,
        release_id="release-1",
        corpus_id="corpus-1",
        release_dir=release_dir,
    )

    def fail_release_safety() -> None:
        raise SmokeError("RELEASE_SAFETY_FAILED")

    result = _execute_smoke(
        context,
        (Stage("fresh_verify", "FRESH_VERIFY_FAILED", fail_release_safety),),
    )

    assert result == 1
    assert not (tmp_path / "artifacts/deployment-handoff.txt").exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error_code"] == "RELEASE_SAFETY_FAILED"
    assert report["release_dir"] is None
    assert report["stages"][0]["name"] == "fresh_verify"
    assert report["stages"][0]["error_code"] == "RELEASE_SAFETY_FAILED"


def test_success_handoff_contains_only_approved_fields(tmp_path: Path) -> None:
    """锁定 handoff 只含发布身份、七文件摘要和 smoke 告警。"""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    context = SmokeContext(
        root=tmp_path,
        report_path=tmp_path / "artifacts/release-smoke-report.json",
        head="a" * 40,
        release_id="release-1",
        corpus_id="corpus-1",
        release_dir=release_dir,
        files=[
            {
                "name": f"delivery-{index}",
                "sha256": f"{index:064x}",
                "size_bytes": index,
            }
            for index in range(7)
        ],
        release_safety={
            "mode": "release",
            "passed": True,
            "violations": 0,
        },
    )

    handoff = _write_handoff(context)

    lines = handoff.read_text(encoding="utf-8").splitlines()
    assert lines[:4] == [
        f"source_revision={'a' * 40}",
        "release_id=release-1",
        "corpus_id=corpus-1",
        f"release_dir={release_dir}",
    ]
    assert len([line for line in lines if line.startswith("file=")]) == 7
    assert lines[-1] == "仅 smoke，不启动 worker，/ready=503 为预期"
    assert "token" not in handoff.read_text(encoding="utf-8").lower()


def test_release_smoke_report_has_complete_identity_and_file_summary(
    tmp_path: Path,
) -> None:
    """锁定报告身份字段和恰好七项交付文件摘要。"""
    report_path = tmp_path / "release-smoke-report.json"
    context = SmokeContext(
        root=tmp_path,
        report_path=report_path,
        head="a" * 40,
        release_id="release-1",
        corpus_id="corpus-1",
        release_dir=tmp_path / "release",
        files=[
            {
                "name": f"delivery-{index}",
                "sha256": f"{index:064x}",
                "size_bytes": index,
            }
            for index in range(7)
        ],
    )

    _write_report(context, {"stages": [], "status": "passed"})

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["head"] == "a" * 40
    assert report["source_revision"] == report["head"]
    assert report["release_id"] == "release-1"
    assert report["corpus_id"] == "corpus-1"
    assert report["release_dir"] == str(tmp_path / "release")
    assert len(report["files"]) == 7
