from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import dev
from tests.adapters.parsers.docx_fixtures import HEADING, PARAGRAPH, build_docx


def test_check_uses_existing_offline_quality_tools() -> None:
    rendered = [" ".join(command) for command in dev._check_commands()]

    assert any(" -m ruff check ." in command for command in rendered)
    assert any(" -m mypy --no-incremental" in command for command in rendered)
    pytest_command = next(
        command for command in rendered if " -m pytest -q" in command
    )
    assert "-m not local_integration and not live_provider" in pytest_command
    assert "--ignore=" not in pytest_command


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


def test_inspect_document_defaults_to_summary_and_explicit_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "sample.docx"
    output = tmp_path / "ir.json"
    source.write_bytes(build_docx(HEADING + PARAGRAPH))

    return_code = dev.main(
        [
            "inspect-document",
            str(source),
            "--output-json",
            str(output),
        ]
    )
    stdout = capsys.readouterr().out

    assert return_code == 0
    assert "document_hash_prefix=" in stdout
    assert "parser=docx-ooxml-v4@" in stdout
    assert "nodes=" in stdout
    assert "安装说明" not in stdout
    assert "安装说明" not in output.read_text(encoding="utf-8")


def test_inspect_document_include_content_is_explicit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.docx"
    output = tmp_path / "ir.json"
    source.write_bytes(build_docx(PARAGRAPH))

    assert (
        dev.main(
            [
                "inspect-document",
                str(source),
                "--output-json",
                str(output),
                "--include-content",
            ]
        )
        == 0
    )
    assert "第一步" in output.read_text(encoding="utf-8")
