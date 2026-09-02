from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("JINA_API_KEY", None)
    environment.pop("DASHSCOPE_API_KEY", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "scripts/dev.py", *arguments],
        cwd=_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_provider_check_is_offline_and_reports_fingerprints() -> None:
    completed = _run(
        "provider-check",
        "--profile",
        "configs/profiles/dev-jina-qwen37-hot-standby.json",
    )
    assert completed.returncode == 0, completed.stderr
    assert "network_calls=0" in completed.stdout
    assert "index_fingerprint=sha256:" in completed.stdout
    assert "serving_fingerprint=sha256:" in completed.stdout


@pytest.mark.parametrize(
    "scenario",
    (
        "jina-timeout",
        "jina-429",
        "jina-bad-dimension",
        "both-unavailable",
    ),
)
def test_failover_smoke_uses_only_injected_providers(scenario: str) -> None:
    completed = _run("failover-smoke", "--scenario", scenario)
    assert completed.returncode == 0, completed.stderr
    assert f"scenario={scenario}" in completed.stdout


def test_live_provider_smoke_requires_explicit_master_switch() -> None:
    completed = _run("provider-smoke", "--provider", "jina")
    assert completed.returncode == 2
    assert "RAG_ALLOW_EXTERNAL_API=true" in completed.stderr
