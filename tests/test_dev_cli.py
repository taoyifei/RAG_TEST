from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from scripts import dev


def test_check_uses_existing_offline_quality_tools() -> None:
    rendered = [" ".join(command) for command in dev._check_commands()]

    assert any(" -m ruff check ." in command for command in rendered)
    assert any(" -m mypy --no-incremental" in command for command in rendered)
    pytest_command = next(
        command for command in rendered if " -m pytest -q" in command
    )
    assert "--ignore=tests/test_qdrant_index.py" in pytest_command


def test_command_runner_returns_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        normalized = tuple(command)
        calls.append(normalized)
        return subprocess.CompletedProcess(
            normalized,
            7 if normalized[-1] == "fail" else 0,
        )

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    return_code = dev._run_commands(
        (("python", "pass"), ("python", "fail"), ("python", "unrun"))
    )

    assert return_code == 7
    assert calls == [("python", "pass"), ("python", "fail")]
