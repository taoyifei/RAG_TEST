"""V8真实评测必须绑定唯一版本并保护仓库外正文。"""

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from types import FunctionType

import pytest

from evaluation.wanshitong.v2 import run_wb08r03g_v8 as runner


def test_runtime_path_observation_does_not_require_a_gold_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner.legacy,
        "read_trace",
        lambda *_args, **_kwargs: (
            {
                "retrieval.claim_publication": [
                    {
                        "answer_path": "LLM_CLAIM_VALIDATED",
                        "publication_path": "MODEL_VALIDATED_CLAIM",
                        "generation_called": True,
                        "accepted_claim_count": 2,
                        "published_claim_count": 2,
                        "final_atom_coverage": [["A1", "PARTIAL"]],
                    }
                ]
            },
            None,
        ),
    )
    row = {
        "truth_status": "NEEDS_TRUTH_REVIEW",
        "answer_path": "NOT_OBSERVED",
        "accepted_claim_count": "NOT_OBSERVED",
    }
    result = runner._enrich([row], {"version_bundle_sha256": "v"})[0]
    assert result["answer_path"] == "LLM_CLAIM_VALIDATED"
    assert result["accepted_claim_count"] == 2
    assert result["final_atom_coverage"] == [["A1", "PARTIAL"]]
    assert result["truth_status"] == "NEEDS_TRUTH_REVIEW"


def _bundle() -> dict[str, str]:
    return {
        **dict.fromkeys(runner._VERSION_FIELDS, "frozen-value"),
        "container": "wanshitong-wb08r01-app",
    }


def test_version_binding_rejects_production_and_missing_observations() -> None:
    for key, value in (
        ("container", "wanshitong-app"),
        ("packet_revision", "NOT_OBSERVED"),
    ):
        bundle = _bundle()
        bundle[key] = value
        with pytest.raises(ValueError):
            runner.bind_version(bundle)


def test_version_change_invalidates_all_previous_gate_identity() -> None:
    first = runner.bind_version(_bundle())
    second = runner.bind_version(
        {**_bundle(), "configuration_sha256": "changed"}
    )
    assert first["version_bundle_sha256"] != second["version_bundle_sha256"]


def test_live_version_change_prevents_quality_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "observe_runtime_guard",
        lambda: {
            "runtime_guard_sha256": "changed",
            "code_sha": "changed",
            "image_id": "changed",
        },
    )
    with pytest.raises(ValueError, match="SERVING_VERSION_CHANGED"):
        runner.verify_runtime(_bundle())


def test_private_output_rejects_repository_and_existing_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="OUTSIDE_REPOSITORY"):
        runner._private_output(runner._REPO / "private-evaluation")
    with pytest.raises(ValueError, match="FRESH_GATE_DIRECTORY_REQUIRED"):
        runner._private_output(tmp_path)


def test_private_output_is_restricted_and_exclusive(tmp_path: Path) -> None:
    directory = runner._private_output(tmp_path / "fresh")
    runner._write(directory / "result.json", {"safe": True})
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "result.json").stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        runner._write(directory / "result.json", {"safe": False})


@pytest.mark.parametrize("gate", ["evidence-pack-12", "concurrency-4"])
def test_every_gate_rejects_reported_semantic_safety_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    version = runner.bind_version(_bundle())
    runner._write(tmp_path / "version.safe.json", version)
    row = {
        "version_bundle_sha256": version["version_bundle_sha256"],
        "run_id": "r1",
        "question_sha256": "question-hash",
        "answer_sha256": "answer-hash",
        "frozen_expected_behavior": "REFUSE",
    }
    runner._write(tmp_path / "observations-v8.safe.json", [row])
    runner._write(
        tmp_path / "summary-v8.safe.json", {"v8_packet_status": "OBSERVED"}
    )
    judgment = {
        **row,
        "case_id": "synthetic",
        "reviewer": "固定离线复现",
        **dict.fromkeys(runner.legacy._REVIEW_FIELDS, 0),
        "unsupported_high_risk_fact_count": 1,
    }
    judgments = tmp_path / "judgments.ndjson"
    judgments.write_text(json.dumps(judgment) + "\n")
    monkeypatch.setattr(
        runner.legacy, "_evidence_pack_12_gate", lambda _rows: "PASSED"
    )
    monkeypatch.setattr(
        runner.legacy, "_concurrency_gate", lambda _rows: {"status": "PASSED"}
    )
    assert (
        runner.review(tmp_path, gate, judgments)["v8_gate_status"] == "FAILED"
    )


def test_provider_configuration_change_changes_runtime_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE knowledge_bases(id TEXT);
        CREATE TABLE retrieval_profile_revisions(id TEXT);
        CREATE TABLE knowledge_base_model_settings(id TEXT);
        CREATE TABLE index_revisions(id TEXT);
        CREATE TABLE provider_connections(id TEXT, configuration TEXT);
        INSERT INTO provider_connections VALUES ('p', 'config-a');
    """)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *_args, **_kwargs: connection
    )

    def run_guard() -> None:
        code = compile(runner._DATABASE_GUARD, "<database-guard>", "exec")
        FunctionType(code, {})()

    with redirect_stdout(io.StringIO()) as first:
        run_guard()
    connection.execute(
        "UPDATE provider_connections SET configuration='config-b'"
    )
    with redirect_stdout(io.StringIO()) as second:
        run_guard()
    assert (
        json.loads(first.getvalue())["database_behavior_sha256"]
        != json.loads(second.getvalue())["database_behavior_sha256"]
    )
