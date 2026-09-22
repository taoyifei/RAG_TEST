"""WB08R-03R2 审计问题必须保持既有冻结题身份。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evaluation.wanshitong.v2.run_wb08r03_candidate import _cases

_ROOT = Path(__file__).resolve().parents[2]
_DATASET = _ROOT / "evaluation" / "wanshitong" / "wb08r03r2-regression-v1"
_SOURCE = _ROOT / "evaluation" / "wanshitong" / "v2"


def test_regression_cases_match_frozen_source() -> None:
    manifest = json.loads((_DATASET / "manifest.json").read_text())
    case_bytes = (_DATASET / "cases.ndjson").read_bytes()
    cases = tuple(json.loads(line) for line in case_bytes.splitlines())
    assert len(cases) == manifest["case_count"]
    assert hashlib.sha256(case_bytes).hexdigest() == manifest["cases_sha256"]
    assert manifest["standard_answers_stored"] is False
    for filename, digest in manifest["supplemental_files_sha256"].items():
        actual = hashlib.sha256((_DATASET / filename).read_bytes())
        assert actual.hexdigest() == digest
    assert {case["case_id"] for case in cases} == set(
        manifest["case_identity"]
    )

    for filename, frozen_digest in manifest["source_files_sha256"].items():
        path = _SOURCE / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen_digest
        originals = {
            row["case_id"]: row
            for row in (
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        }
        for case in (
            item for item in cases if item["source_dataset"] == filename
        ):
            source = originals[case["case_id"]]
            for field in (
                "question",
                "question_sha256",
                "question_style",
                "expected_behavior",
            ):
                assert case[field] == source[field]
            assert case["context_question"] == source.get("context_question")
            assert case["question_sha256"] == hashlib.sha256(
                case["question"].encode()
            ).hexdigest()
            assert case["question_sha256"] == manifest["case_identity"][
                case["case_id"]
            ]
            assert "answer" not in case
            assert "expected_answer" not in case


def test_regression_audit_is_outside_runtime_image() -> None:
    ignored = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "evaluation/wanshitong/**" in ignored
    assert "evaluation/wanshitong" not in (_ROOT / "Dockerfile").read_text(
        encoding="utf-8"
    )


def test_gate_selections_do_not_claim_unverified_expectations() -> None:
    selection = json.loads((_DATASET / "gate-selections.json").read_text())
    sources = {
        row["case_id"]: (filename, row)
        for filename in (
            "formal-54.ndjson",
            "natural-60.ndjson",
            "adversarial-30.ndjson",
        )
        for row in (
            json.loads(line)
            for line in (_SOURCE / filename)
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }
    for gate in selection["gate_selections"].values():
        assert gate["functional_gate_status"] == "NOT_OBSERVED"
        assert len(gate["cases"]) + sum(
            gate["missing_fixture_slots"].values()
        ) == gate["required_count"]
        assert gate["unverified_expectations"]
        for case in gate["cases"]:
            filename, original = sources[case["case_id"]]
            assert case["source_dataset"] == filename
            assert case["question_sha256"] == original["question_sha256"]
            assert case["expected_behavior"] == original["expected_behavior"]
            assert "question" not in case
            assert "answer" not in case
        selected = _cases(
            frozenset(case["case_id"] for case in gate["cases"])
        )
        assert len(selected) == len(gate["cases"])
