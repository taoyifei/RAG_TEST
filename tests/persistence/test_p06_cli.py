from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.dev import main
from tests.adapters.parsers.docx_fixtures import TABLE, build_docx

_PROFILE = Path("configs/profiles/dev-p06-memory.json")


def test_p06_cli_initializes_ingests_reports_and_plans_gc(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            (
                "init-data",
                "--profile",
                str(_PROFILE),
                "--data-dir",
                str(tmp_path),
            )
        )
        == 0
    )
    initial = json.loads(capsys.readouterr().out)
    document = tmp_path / "synthetic.docx"
    document.write_bytes(build_docx(TABLE))
    assert (
        main(
            (
                "ingest",
                initial["knowledge_base_id"],
                str(document),
                "--document-id",
                "cli-document",
                "--profile",
                str(_PROFILE),
                "--data-dir",
                str(tmp_path),
            )
        )
        == 0
    )
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["state"] == "active"
    assert str(tmp_path) not in json.dumps(ingested)
    second_document = tmp_path / "synthetic-copy.docx"
    second_document.write_bytes(document.read_bytes())
    assert (
        main(
            (
                "ingest",
                initial["knowledge_base_id"],
                str(second_document),
                "--document-id",
                "cli-document-copy",
                "--profile",
                str(_PROFILE),
                "--data-dir",
                str(tmp_path),
            )
        )
        == 0
    )
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["document_count"] == 2
    assert (
        main(
            (
                "index-info",
                initial["knowledge_base_id"],
                "--profile",
                str(_PROFILE),
                "--data-dir",
                str(tmp_path),
            )
        )
        == 0
    )
    info = json.loads(capsys.readouterr().out)
    assert info["active_revision_id"] == ingested["revision_id"]
    assert (
        main(
            (
                "index-gc",
                "--plan",
                "--profile",
                str(_PROFILE),
                "--data-dir",
                str(tmp_path),
            )
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "dry-run"
    assert str(tmp_path) not in json.dumps(plan)
