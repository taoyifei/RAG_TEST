"""校验湾事通 V2 题集的身份、分布和冻结摘要。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

DATASET_ROOT = Path(__file__).resolve().parent
DATASET_REVISION = "wanshitong-v2-20260917"
CASE_FILES = {
    "formal-54.ndjson": 54,
    "natural-60.ndjson": 60,
    "adversarial-30.ndjson": 30,
    "latency-24.ndjson": 24,
}
NATURAL_STYLES = {
    "short_ellipsis": 18,
    "colloquial": 15,
    "typo_abbreviation": 9,
    "multi_turn": 9,
    "compound": 9,
}
ADVERSARIAL_CATEGORIES = {
    "missing_or_current_source": 6,
    "false_premise": 6,
    "force_without_source": 6,
    "unindexed_template_body": 6,
    "ambiguous_scope": 6,
}
LATENCY_BUCKETS = {
    "catalog_navigation": 8,
    "single_fact": 8,
    "compound_multi_turn": 8,
}
PERSONAS = frozenset(
    {"new_employee", "employee", "project_manager", "tech_operations"}
)
BEHAVIORS = frozenset({"ANSWER", "LIMITED", "CLARIFY", "REFUSE"})
ATOM_LABELS = frozenset(
    {
        "source_identity",
        "actor",
        "action",
        "condition",
        "time_limit",
        "quantity",
        "exception",
        "negation",
        "enumeration",
        "sequence",
        "comparison",
        "existence",
        "reference",
        "scope",
        "ambiguity",
        "refusal_boundary",
    }
)
REQUIRED_CASE_FIELDS = frozenset(
    {
        "dataset_revision",
        "case_id",
        "question",
        "question_sha256",
        "seed_case_id",
        "question_style",
        "persona",
        "intent_identity",
        "expected_source_document",
        "expected_behavior",
        "required_atoms",
    }
)
OPTIONAL_CASE_FIELDS = frozenset(
    {"adversarial_category", "context_question", "latency_bucket"}
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 JSON：{path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path.name}")
    return value


def _read_cases(path: Path, expected_count: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"无法读取题集：{path.name}") from error
    if len(lines) != expected_count or any(not line.strip() for line in lines):
        raise ValueError(f"题集条数或空行异常：{path.name}")
    cases = []
    for line_number, line in enumerate(lines, 1):
        try:
            case = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"题集 JSON 无效：{path.name}:{line_number}") from error
        if not isinstance(case, dict):
            raise ValueError(f"Case 必须是对象：{path.name}:{line_number}")
        _validate_case(case, path.name, line_number)
        cases.append(case)
    return cases


def _validate_case(case: dict[str, Any], filename: str, line_number: int) -> None:
    location = f"{filename}:{line_number}"
    if REQUIRED_CASE_FIELDS - case.keys() or case.keys() - (
        REQUIRED_CASE_FIELDS | OPTIONAL_CASE_FIELDS
    ):
        raise ValueError(f"Case 字段不符合合同：{location}")
    if case["dataset_revision"] != DATASET_REVISION:
        raise ValueError(f"数据集版本不匹配：{location}")
    if not all(
        isinstance(case[key], str) and case[key].strip()
        for key in (
            "case_id",
            "question",
            "question_sha256",
            "seed_case_id",
            "question_style",
            "persona",
            "intent_identity",
        )
    ):
        raise ValueError(f"Case 身份字段无效：{location}")
    if case["question"] != case["question"].strip():
        raise ValueError(f"问题首尾空白会改变身份：{location}")
    question_hash = _sha256(case["question"].encode("utf-8"))
    if case["question_sha256"] != question_hash:
        raise ValueError(f"同 ID 问题摘要不匹配：{location}")
    source = case["expected_source_document"]
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise ValueError(f"预期来源无效：{location}")
    if case["expected_behavior"] not in BEHAVIORS:
        raise ValueError(f"预期行为无效：{location}")
    atoms = case["required_atoms"]
    if (
        not isinstance(atoms, list)
        or not atoms
        or any(not isinstance(atom, str) for atom in atoms)
        or len(atoms) != len(set(atoms))
    ):
        raise ValueError(f"事实原子标签无效：{location}")
    if not set(atoms) <= ATOM_LABELS:
        raise ValueError(f"事实原子包含非抽象标签：{location}")
    if case["persona"] not in PERSONAS:
        raise ValueError(f"用户角色无效：{location}")
    if "context_question" in case and (
        not isinstance(case["context_question"], str)
        or not case["context_question"].strip()
    ):
        raise ValueError(f"多轮上下文无效：{location}")


def validate_dataset(root: Path = DATASET_ROOT) -> dict[str, Any]:
    """读取冻结资料并拒绝身份漂移、分布变化及文件篡改。

    Args:
        root: V2 评测目录。

    Returns:
        已核验的 Manifest 与四份 Case。

    Raises:
        ValueError: 冻结合同、身份、摘要或分布不满足要求。
    """
    manifest = _read_json(root / "dataset-manifest.json")
    if manifest.get("dataset_revision") != DATASET_REVISION:
        raise ValueError("Manifest 数据集版本不匹配")
    if set(manifest.get("files", {})) != set(CASE_FILES):
        raise ValueError("Manifest 题集文件不完整")

    datasets = {}
    for filename, expected_count in CASE_FILES.items():
        path = root / filename
        cases = _read_cases(path, expected_count)
        recorded = manifest["files"][filename]
        if recorded.get("case_count") != expected_count or recorded.get(
            "sha256"
        ) != _sha256(path.read_bytes()):
            raise ValueError(f"冻结题集摘要不匹配：{filename}")
        datasets[filename] = cases

    base_cases = [
        *datasets["formal-54.ndjson"],
        *datasets["natural-60.ndjson"],
        *datasets["adversarial-30.ndjson"],
    ]
    case_ids = [case["case_id"] for case in base_cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("冻结题集 case_id 重复")
    identities = {case["case_id"]: case["question_sha256"] for case in base_cases}
    if manifest.get("case_identity") != identities:
        raise ValueError("同 ID 换题或身份锁不匹配")
    _validate_distribution(datasets)
    return {"manifest": manifest, "datasets": datasets}


def _validate_distribution(datasets: dict[str, list[dict[str, Any]]]) -> None:
    formal = datasets["formal-54.ndjson"]
    natural = datasets["natural-60.ndjson"]
    adversarial = datasets["adversarial-30.ndjson"]
    latency = datasets["latency-24.ndjson"]
    formal_ids = {case["case_id"] for case in formal}
    if any(not case["case_id"].startswith("WB08R-F-") for case in formal):
        raise ValueError("Formal 必须使用新的不可变 ID")
    if Counter(case["expected_behavior"] for case in formal) != {
        "ANSWER": 37,
        "LIMITED": 2,
        "REFUSE": 15,
    }:
        raise ValueError("Formal-54 行为分布不符合旧版合同")
    if Counter(case["question_style"] for case in formal) != {
        "formal_diagnostic": 17,
        "formal_limited": 2,
        "formal_refusal": 15,
        "formal_regression": 20,
    }:
        raise ValueError("Formal-54 分组不符合旧版合同")
    if Counter(case["question_style"] for case in natural) != NATURAL_STYLES:
        raise ValueError("Natural-60 问法分布不符合设计")
    if sum(len(case["question"]) <= 18 for case in natural) < 30:
        raise ValueError("Natural-60 短问不足 30 条")
    if any(case["seed_case_id"] not in formal_ids for case in natural):
        raise ValueError("Natural Seed 不在 Formal-54")
    formal_by_id = {case["case_id"]: case for case in formal}
    if any(
        case["intent_identity"]
        != formal_by_id[case["seed_case_id"]]["intent_identity"]
        or case["expected_source_document"]
        != formal_by_id[case["seed_case_id"]]["expected_source_document"]
        for case in natural
    ):
        raise ValueError("Natural 意图或来源与 Seed 不一致")
    if any(
        case["expected_source_document"]
        and case["expected_source_document"] in case["question"]
        for case in natural
    ):
        raise ValueError("Natural 问题包含完整来源标题")
    if any(
        (case["question_style"] == "multi_turn")
        != ("context_question" in case)
        for case in natural
    ):
        raise ValueError("多轮追问缺少前一轮问题")
    if Counter(case.get("adversarial_category") for case in adversarial) != (
        ADVERSARIAL_CATEGORIES
    ):
        raise ValueError("Adversarial-30 类别分布不符合设计")
    if any(case["seed_case_id"] not in formal_ids for case in adversarial):
        raise ValueError("Adversarial Seed 不在 Formal-54")
    if Counter(case.get("latency_bucket") for case in latency) != LATENCY_BUCKETS:
        raise ValueError("Latency-24 类型分布不符合设计")
    base_by_id = {case["case_id"]: case for case in [*formal, *natural]}
    if len({case["case_id"] for case in latency}) != len(latency):
        raise ValueError("Latency-24 包含重复 ID")
    for case in latency:
        comparable = {key: value for key, value in case.items() if key != "latency_bucket"}
        if base_by_id.get(case["case_id"]) != comparable:
            raise ValueError(f"Latency 题目与主集不一致：{case['case_id']}")


def assert_stable_ids(
    previous_manifest: dict[str, Any], current_manifest: dict[str, Any]
) -> None:
    """跨版本检查复用 ID 是否仍指向原问题。"""
    before = previous_manifest["case_identity"]
    after = current_manifest["case_identity"]
    changed = {
        case_id
        for case_id in before.keys() & after.keys()
        if before[case_id] != after[case_id]
    }
    if changed:
        raise ValueError(f"同 ID 换题：{sorted(changed)}")
