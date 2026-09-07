from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import dev
from tests.adapters.parsers.docx_fixtures import HEADING, PARAGRAPH, build_docx


def test_wsl_browser_acceptance_uses_requested_candidate_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def browser_run(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        calls.append(environment)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("P10_EXTERNAL_SERVER", "1")
    monkeypatch.setenv("P10_BASE_URL", "http://127.0.0.1:38119")
    monkeypatch.setattr(dev, "_run_web_script", lambda _script: 0)
    monkeypatch.setattr(dev.shutil, "which", lambda _name: "node.exe")
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(dev, "_windows_path", str)
    monkeypatch.setattr(dev.subprocess, "run", browser_run)
    monkeypatch.setattr(
        dev.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("已有候选实例不得另起测试服务"),
    )

    assert dev._web_e2e(None) == 0
    assert len(calls) == 1
    assert calls[0]["P10_BASE_URL"] == "http://127.0.0.1:38119"
    assert "P10_BASE_URL/w" in calls[0]["WSLENV"]


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


def test_chunk_document_defaults_to_statistics_without_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "sample.docx"
    source.write_bytes(build_docx(HEADING + PARAGRAPH))

    assert dev.main(["chunk-document", str(source)]) == 0
    stdout = capsys.readouterr().out

    assert "chunker=docx-structural-v3" in stdout
    assert "chunk_id_prefixes=" in stdout
    assert "第一步" not in stdout


def test_chunk_ablation_writes_provisional_json_and_markdown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.docx"
    output = tmp_path / "ablation"
    source.write_bytes(build_docx(PARAGRAPH))

    assert (
        dev.main(
            [
                "chunk-ablation",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    rows = json.loads(
        (output / "chunk-ablation.json").read_text(encoding="utf-8")
    )
    markdown = (output / "chunk-ablation.md").read_text(encoding="utf-8")
    assert len(rows) == 3
    assert all(row["provisional"] is True for row in rows)
    assert "P08" in markdown
    assert "不选择最佳参数" in markdown
    assert all("selected_candidate" not in row for row in rows)
