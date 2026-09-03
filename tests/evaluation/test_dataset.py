"""P08 数据集 Schema、Group Split 和身份回归。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evaluation.v2.dataset import load_dataset_directory
from evaluation.v2.runtime import validate_fixture_identities

_DATASET = Path("evaluation/datasets/synthetic")


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "synthetic"
    shutil.copytree(_DATASET, target)
    return target


def test_synthetic_dataset_is_versioned_and_group_isolated() -> None:
    dataset = load_dataset_directory(_DATASET)

    assert dataset.manifest.schema_version == "3"
    assert dataset.manifest.content_classification == "synthetic_public"
    assert len(dataset.cases) == 52
    assert len(dataset.tuning_cases()) == 28
    assert len(dataset.holdout_cases()) == 24
    tuning_groups = {case.group_id for case in dataset.tuning_cases()}
    holdout_groups = {case.group_id for case in dataset.holdout_cases()}
    assert tuning_groups.isdisjoint(holdout_groups)


def test_invalid_case_schema_fails_the_whole_dataset(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    with (dataset / "cases.jsonl").open("a", encoding="utf-8") as output:
        output.write('{"schema_version":"2","case_id":"invalid"}\n')

    with pytest.raises(ValueError, match="Case"):
        load_dataset_directory(dataset)


def test_group_leakage_is_rejected(tmp_path: Path) -> None:
    dataset = _copy_dataset(tmp_path)
    case_path = dataset / "cases.jsonl"
    payloads = [json.loads(line) for line in case_path.read_text().splitlines()]
    holdout = next(item for item in payloads if item["split"] == "holdout")
    holdout["group_id"] = payloads[0]["group_id"]
    case_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in payloads)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Group"):
        load_dataset_directory(dataset)


def test_same_bytes_keep_distinct_logical_document_versions() -> None:
    checks = validate_fixture_identities(load_dataset_directory(_DATASET))

    assert checks["shared_byte_groups"] >= 1
    assert checks["content_change_documents"] >= 1
    assert checks["versions"] > checks["documents"]
