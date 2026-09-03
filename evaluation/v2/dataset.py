"""P08 数据集加载、摘要和 Group Split 隔离。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from evaluation.v2.fixtures import fixture_coverage_tags
from evaluation.v2.models import DatasetManifest, EvaluationCase
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import DocumentRef, validate_document_ref_uniqueness


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    """已验证的 Manifest、Case 和稳定摘要。"""

    manifest: DatasetManifest
    cases: tuple[EvaluationCase, ...]
    dataset_sha256: str

    def tuning_cases(self) -> tuple[EvaluationCase, ...]:
        """只返回允许参数搜索读取标签的 tuning Case。

        Args:
            无参数；读取当前数据集。

        Returns:
            保持文件顺序的 tuning Case。

        """
        return tuple(case for case in self.cases if case.split == "tuning")

    def holdout_cases(self) -> tuple[EvaluationCase, ...]:
        """返回仅供固定候选最终评测的 holdout Case。

        Args:
            无参数；读取当前数据集。

        Returns:
            保持文件顺序的 holdout Case。

        """
        return tuple(case for case in self.cases if case.split == "holdout")


def load_dataset_directory(path: Path) -> LoadedDataset:
    """读取目录并执行 Schema、身份、覆盖和 Group Split 校验。

    Args:
        path: 含 `manifest.json` 和 `cases.jsonl` 的数据集目录。

    Returns:
        已冻结且带规范摘要的数据集。

    Raises:
        ValueError: 文件、JSON、Schema 或隔离规则不满足。

    """
    root = path.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = DatasetManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as error:
        raise ValueError("P08 dataset manifest 无效。") from error
    cases = _load_cases(root / manifest.cases_file)
    _validate_dataset(manifest, cases)
    digest = canonical_sha256(
        {
            "manifest": manifest.model_dump(mode="json"),
            "cases": [case.model_dump(mode="json") for case in cases],
        }
    )
    return LoadedDataset(manifest, cases, digest)


def dataset_file_sha256(path: Path) -> str:
    """计算数据集文件的原始字节摘要。

    Args:
        path: 待计算的普通文件。

    Returns:
        带 `sha256:` 前缀的摘要。

    """
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _load_cases(path: Path) -> tuple[EvaluationCase, ...]:
    cases: list[EvaluationCase] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError("P08 cases 文件无法读取。") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
            cases.append(EvaluationCase.model_validate(decoded))
        except (json.JSONDecodeError, ValidationError) as error:
            raise ValueError(f"P08 Case 第 {line_number} 行无效。") from error
    if not cases:
        raise ValueError("P08 数据集至少需要一个 Case。")
    return tuple(cases)


def _validate_dataset(
    manifest: DatasetManifest,
    cases: tuple[EvaluationCase, ...],
) -> None:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("P08 数据集包含重复 case_id。")
    documents = manifest.documents
    document_ids = [document.document_id for document in documents]
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("P08 Manifest 包含重复 document_id。")
    validate_document_ref_uniqueness(
        [
            DocumentRef(
                project_id=document.project_id,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.document_id,
                display_name=document.versions[-1].display_name,
            )
            for document in documents
        ]
    )
    known_documents = set(document_ids)
    known_scopes = {
        (document.project_id, document.knowledge_base_id)
        for document in documents
    }
    for case in cases:
        if (case.project_id, case.knowledge_base_id) not in known_scopes:
            raise ValueError(f"{case.case_id}: Case scope 不在 Manifest。")
        referenced = set(case.expected.relevant_document_ids)
        referenced.update(case.constraints.forbidden_document_ids)
        referenced.update(
            item.document_id for item in case.expected.required_source_ranges
        )
        if not referenced <= known_documents:
            raise ValueError(f"{case.case_id}: Case 引用了未知 document_id。")
    _validate_group_isolation(manifest, cases)
    _validate_category_coverage(cases)
    _validate_p08_5_coverage(cases)


def _validate_group_isolation(
    manifest: DatasetManifest,
    cases: tuple[EvaluationCase, ...],
) -> None:
    splits_by_group: dict[str, set[str]] = {}
    for case in cases:
        splits_by_group.setdefault(case.group_id, set()).add(case.split)
    leaking = sorted(
        group for group, splits in splits_by_group.items() if len(splits) > 1
    )
    if leaking:
        raise ValueError(f"P08 tuning/holdout Group 泄漏：{leaking}")
    document_groups = {
        document.document_id: document.family_group_id
        for document in manifest.documents
    }
    for case in cases:
        for document_id in case.expected.relevant_document_ids:
            if document_groups[document_id] != case.group_id:
                raise ValueError(
                    f"{case.case_id}: 文档族与 Case group_id 不一致。"
                )
    _validate_fixture_coverage(manifest)


def _validate_category_coverage(cases: tuple[EvaluationCase, ...]) -> None:
    required = {
        "document_identity",
        "hierarchy",
        "table_structure",
        "exact_identifier",
        "negative_refusal",
        "revision_isolation",
        "scope_isolation",
        "routing_failure",
    }
    categories = {case.category for case in cases}
    missing = sorted(required - categories)
    if missing:
        raise ValueError(f"P08 数据集缺少必需类别：{missing}")
    if not any(case.split == "tuning" for case in cases):
        raise ValueError("P08 数据集缺少 tuning Case。")
    if not any(case.split == "holdout" for case in cases):
        raise ValueError("P08 数据集缺少 holdout Case。")


def _validate_p08_5_coverage(cases: tuple[EvaluationCase, ...]) -> None:
    counters = {
        "total": len(cases),
        "cjk_phrase": sum(case.category == "cjk_phrase" for case in cases),
        "identifier_free_fact": sum(
            case.expected.answerable
            and not case.expected.required_identifiers
            and any("\u3400" <= char <= "\u9fff" for char in case.query)
            for case in cases
        ),
        "cjk_noise": sum(case.category == "cjk_noise" for case in cases),
        "duplicate_source_range": sum(
            case.category == "source_range_duplicate" for case in cases
        ),
        "multi_document": sum(
            len(case.expected.relevant_document_ids) > 1 for case in cases
        ),
        "table": sum(case.category == "table_structure" for case in cases),
        "revision_scope_vector": sum(
            case.category
            in {"revision_isolation", "scope_isolation", "routing_failure"}
            for case in cases
        ),
        "unanswerable": sum(
            not case.expected.answerable for case in cases
        ),
        "long_cross_chunk": sum(
            case.category == "long_cross_chunk"
            or case.case_id == "eval_hierarchy_long"
            for case in cases
        ),
    }
    minimums = {
        "total": 50,
        "cjk_phrase": 8,
        "identifier_free_fact": 8,
        "cjk_noise": 6,
        "duplicate_source_range": 4,
        "multi_document": 4,
        "table": 8,
        "revision_scope_vector": 6,
        "unanswerable": 8,
        "long_cross_chunk": 4,
    }
    missing = {
        name: (counters[name], minimum)
        for name, minimum in minimums.items()
        if counters[name] < minimum
    }
    if missing:
        raise ValueError(f"P08.5 数据集覆盖不足：{missing}")


def _validate_fixture_coverage(manifest: DatasetManifest) -> None:
    observed: set[str] = set()
    for document in manifest.documents:
        version_tags: set[str] = set()
        for version in document.versions:
            try:
                version_tags.update(fixture_coverage_tags(version.fixture_id))
            except KeyError as error:
                raise ValueError(
                    f"未知合成 fixture：{version.fixture_id}"
                ) from error
        if set(document.coverage_tags) != version_tags:
            raise ValueError(
                f"{document.document_id}: coverage_tags 与 fixture 不一致。"
            )
        observed.update(version_tags)
    required = {
        "same_bytes_different_document_id",
        "rename_only",
        "content_version_change",
        "same_name_different_content",
        "similar_document_other_kb",
        "short_paragraph",
        "long_paragraph_cross_chunk",
        "multilevel_heading",
        "body_before_heading",
        "numbered_list_restart",
        "footnote_endnote",
        "text_box",
        "table_exact_row",
        "table_middle_empty",
        "table_edge_empty",
        "table_grid_before_after",
        "table_grid_span",
        "table_vmerge",
        "table_multirow_header",
        "nested_table",
        "numeric_unit_negative_percent_date",
        "identifier_standard",
        "identifier_hyphen",
        "identifier_mixed_language",
        "identifier_nfkc_case",
        "identifier_near_miss",
        "knowledge_base_no_answer",
        "topic_similar_unsupported",
        "conflicting_evidence",
        "wrong_document",
        "active_revision",
    }
    missing = sorted(required - observed)
    if missing:
        raise ValueError(f"P08 fixture 覆盖不足：{missing}")


__all__ = [
    "LoadedDataset",
    "dataset_file_sha256",
    "load_dataset_directory",
]
