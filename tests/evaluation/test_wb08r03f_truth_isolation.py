"""F0 来源真值必须可校验，且不能进入生产代码或镜像。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from evaluation.wanshitong.v2.run_wb08r03f_candidate import load_truth

_ROOT = Path(__file__).resolve().parents[2]
_TRUTH = _ROOT / "evaluation" / "wanshitong" / "wb08r03f-truth-v1"
_FROZEN = _ROOT / "evaluation" / "wanshitong" / "v2"


def test_truth_has_checked_gold_for_all_failed_cases() -> None:
    manifest, cases, supports = load_truth()

    assert manifest["case_count"] == 24
    assert manifest["verified_case_count"] == 24
    assert manifest["needs_truth_review_count"] == 0
    assert len(supports) == 76
    assert all(row["truth_status"] == "VERIFIED" for row in cases.values())
    assert {
        case_id: cases[case_id]["expected_behavior"]
        for case_id in (
            "WB08R-F-017",
            "WB08R-N-048",
            "WB08R-N-050",
            "WB08R-N-057",
            "WB08R-N-058",
        )
    } == {
        "WB08R-F-017": "ANSWER",
        "WB08R-N-048": "LIMITED",
        "WB08R-N-050": "ANSWER",
        "WB08R-N-057": "ANSWER",
        "WB08R-N-058": "ANSWER",
    }
    assert cases["WB08R-N-055"]["expected_behavior"] == "LIMITED"
    for support in supports.values():
        assert support["quote_sha256"] == (
            "sha256:" + hashlib.sha256(support["quote"].encode()).hexdigest()
        )
        assert support["locator"]
        assert support["node_id"]


def test_table_gold_keeps_row_and_cell_identity() -> None:
    _, _, supports = load_truth()
    for case_id in ("WB08R-F-015", "WB08R-N-056"):
        rows = [row for row in supports.values() if row["case_id"] == case_id]
        assert {row["table_row_index"] for row in rows} == {2}
        assert len({row["table_node_id"] for row in rows}) == 1
        assert {row["table_cell"] for row in rows} == {
            "tc:0",
            "tc:2",
            "tc:4",
        }

    for case_id in ("WB08R-F-017", "WB08R-N-048", "WB08R-N-057"):
        rows = [row for row in supports.values() if row["case_id"] == case_id]
        assert {row["table_row_index"] for row in rows} == {2}
        assert {row["table_node_id"] for row in rows} == {
            "node_dee6320820698d600d4826eb9aaf139a"
        }
        assert {row["table_cell"] for row in rows} == (
            {"tc:0", "tc:2"} if case_id == "WB08R-N-048"
            else {"tc:0", "tc:1", "tc:2"}
        )
        for row in rows:
            if row["table_cell"] == "tc:1":
                assert row["locator"] == "body/tbl:113/tr:1/tc:1/p:1"
                assert row["span_type"] == "repeated_context"
            if row["table_cell"] == "tc:2":
                assert row["locator"] == "body/tbl:113/tr:2/tc:2/p:1"
                assert row["quote"] == "30分钟"

    for case_id in ("WB08R-N-050", "WB08R-N-058"):
        rows = [row for row in supports.values() if row["case_id"] == case_id]
        assert {row["table_row_index"] for row in rows} == {1}
        assert {row["table_cell"] for row in rows} == {"tc:1"}
        assert {
            int(row["locator"].split("/")[1].removeprefix("tbl:"))
            for row in rows
        } == {13, 16, 19, 22}
    boundary = [
        row for row in supports.values() if row["case_id"] == "WB08R-N-058"
        and row["locator"] == "body/tbl:22/tr:1/tc:1/p:8"
    ]
    assert {row["quote"] for row in boundary} == {
        "首单交付正式完成，开发团队对该版本的交付责任终止",
        "后续规模化交付由运维团队主导",
    }

    design_rows = [
        row for row in supports.values() if row["case_id"] == "WB08R-N-060"
    ]
    assert {row["table_row_index"] for row in design_rows} == {2}
    assert len({row["table_node_id"] for row in design_rows}) == 1


def test_modified_quote_digest_is_rejected(tmp_path: Path) -> None:
    for filename in ("manifest.json", "cases.ndjson", "evidence_truth.ndjson"):
        shutil.copyfile(_TRUTH / filename, tmp_path / filename)
    evidence_path = tmp_path / "evidence_truth.ndjson"
    evidence_path.write_text(
        evidence_path.read_text(encoding="utf-8").replace(
            "院行政管理机构", "其他部门"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="TRUTH_DIGEST_MISMATCH"):
        load_truth(tmp_path)


def test_truth_is_excluded_from_runtime_and_docker_context() -> None:
    for filename in (".dockerignore", "Dockerfile.dockerignore"):
        lines = (_ROOT / filename).read_text(encoding="utf-8").splitlines()
        assert "evaluation/wanshitong/**" in lines
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ." not in dockerfile
    assert "evaluation/wanshitong" not in dockerfile

    frozen: dict[str, str] = {}
    for filename in (
        "formal-54.ndjson",
        "natural-60.ndjson",
        "latency-24.ndjson",
        "adversarial-30.ndjson",
    ):
        lines = (_FROZEN / filename).read_text(encoding="utf-8").splitlines()
        for line in lines:
            case = json.loads(line)
            frozen[case["case_id"]] = case["question"]
    assert len(frozen) == 144
    load_truth()
    runtime_files = [
        path
        for base in (_ROOT / "src", _ROOT / "frontend" / "src")
        for path in base.rglob("*")
        if path.suffix in {".py", ".ts", ".tsx", ".js", ".jsx"}
    ]
    leaks: list[str] = []
    for path in runtime_files:
        source = path.read_text(encoding="utf-8")
        if "expected_source_document" in source:
            leaks.append(f"{path}: expected_source_document")
        leaks.extend(
            f"{path}: {case_id}"
            for case_id in frozen
            if case_id in source or frozen[case_id] in source
        )
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(
                    name.startswith("evaluation.wanshitong") for name in names
                ):
                    leaks.append(f"{path}: runtime import")
        elif re.search(
            r"\b(?:from|import|require)\b[^;\n]*evaluation/wanshitong",
            source,
        ):
            leaks.append(f"{path}: frontend import")
    assert not leaks, "\n".join(leaks)
