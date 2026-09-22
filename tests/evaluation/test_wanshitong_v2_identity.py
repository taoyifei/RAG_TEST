"""湾事通 V2 题目身份与不可变摘要回归。"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evaluation.wanshitong.v2.validation import (
    DATASET_ROOT,
    assert_stable_ids,
    validate_dataset,
)


def test_v2_frozen_identity_and_file_digests() -> None:
    loaded = validate_dataset()

    assert len(loaded["manifest"]["case_identity"]) == 144
    assert all(
        case["question_sha256"]
        == loaded["manifest"]["case_identity"][case["case_id"]]
        for filename, cases in loaded["datasets"].items()
        if filename != "latency-24.ndjson"
        for case in cases
    )


def test_same_id_new_question_fails_even_after_row_hash_update(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "v2"
    shutil.copytree(DATASET_ROOT, dataset)
    case_path = dataset / "natural-60.ndjson"
    rows = [json.loads(line) for line in case_path.read_text(encoding="utf-8").splitlines()]
    old_manifest = json.loads(
        (dataset / "dataset-manifest.json").read_text(encoding="utf-8")
    )
    rows[0]["question"] = "同一个 ID 被换成另一道问题？"
    rows[0]["question_sha256"] = hashlib.sha256(
        rows[0]["question"].encode("utf-8")
    ).hexdigest()
    case_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="摘要不匹配"):
        validate_dataset(dataset)
    old_manifest["files"]["natural-60.ndjson"]["sha256"] = hashlib.sha256(
        case_path.read_bytes()
    ).hexdigest()
    (dataset / "dataset-manifest.json").write_text(
        json.dumps(old_manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="身份锁不匹配"):
        validate_dataset(dataset)
    changed_manifest = copy.deepcopy(old_manifest)
    changed_manifest["case_identity"][rows[0]["case_id"]] = rows[0][
        "question_sha256"
    ]
    with pytest.raises(ValueError, match="同 ID 换题"):
        assert_stable_ids(old_manifest, changed_manifest)


def test_baseline_observations_use_the_unified_metric_contract() -> None:
    schema = json.loads((DATASET_ROOT / "schema.json").read_text(encoding="utf-8"))
    baseline = json.loads(
        (DATASET_ROOT / "baseline-trace-analysis.json").read_text(encoding="utf-8")
    )
    metric_keys = set(schema["$defs"]["metric_record"]["required"])

    assert len(metric_keys) == 25
    assert len(baseline["observations"]) == 2
    assert all(
        set(observation["metrics"]) == metric_keys
        for observation in baseline["observations"]
    )
