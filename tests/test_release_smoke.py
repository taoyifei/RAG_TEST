"""验证本地 smoke release 浅层编排器。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release_smoke import SmokeError, Stage, execute_stages


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

    assert len(source.splitlines()) <= 520
    for marker in (
        "prepare_runtime_wheels.py",
        "docker buildx build",
        "--network none",
        "asset-selfcheck",
        "freeze_corpus_manifest.py",
        "deployment/package.sh",
        "offline_bundle.py",
        "verify-offline.sh",
        "release-smoke-report.json",
    ):
        assert marker in source
    for forbidden in ("ssh ", "scp ", "docker push", ".57", ".58", ".60"):
        assert forbidden not in source
