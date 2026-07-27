"""人工冻结问答集的严格 schema、切分隔离与原文核验。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_app.parsers.docx import DocxParser

__all__ = [
    "EvaluationCase",
    "EvaluationDataset",
    "HoldoutQuestion",
    "load_dataset",
    "load_holdout_questions",
    "load_tuning_cases",
    "verify_source_evidence",
]

_REQUIRED_CATEGORIES = {
    "ordinary",
    "numeric",
    "table",
    "ocr",
    "cross_chunk",
    "rewrite",
    "multiturn",
    "conflict",
    "unanswerable",
    "prompt_injection",
}
_MINIMUM_CASES = 50
_MINIMUM_HOLDOUT = 15
_MINIMUM_OCR_CASES = 5


class EvidenceLabel(BaseModel):
    """一条人工核对过的来源定位与逐字片段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document: str
    locator_contains: str = Field(min_length=1)
    quote: str | None


class ExpectedLabel(BaseModel):
    """人工验收标准，不交给任何生成模型自评。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answerable: bool | None
    required_facts: tuple[str, ...]
    refusal_code: str | None = None
    evidence: tuple[EvidenceLabel, ...]


class EvaluationCase(BaseModel):
    """一条冻结问题及人工标签。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^Q[0-9]{3}$")
    split: str = Field(pattern=r"^(tuning|holdout)$")
    categories: tuple[str, ...] = Field(min_length=1)
    history_questions: tuple[str, ...] = ()
    question: str = Field(min_length=1)
    validation_state: str = "verified_text"
    expected: ExpectedLabel

    @model_validator(mode="after")
    def _validate_label_state(self) -> EvaluationCase:
        if self.validation_state == "blocked_gpu_ocr":
            if "ocr" not in self.categories:
                raise ValueError("GPU OCR 阻塞题必须属于 ocr 类别。")
            if self.expected.answerable is not None:
                raise ValueError("未实测 OCR 题不得预先声明可回答性。")
            return self
        if self.validation_state != "verified_text":
            raise ValueError("未知题目验证状态。")
        if self.expected.answerable is None:
            raise ValueError("文本题必须冻结可回答性。")
        if self.expected.answerable and not self.expected.evidence:
            raise ValueError("可回答文本题至少需要一条证据。")
        if not self.expected.answerable and self.expected.evidence:
            raise ValueError("不可回答题不得绑定伪证据。")
        return self


class EvaluationDataset(BaseModel):
    """至少 50 题且 holdout 不少于 15 题的冻结集。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: str
    review_method: str
    documents: dict[str, str]
    cases: tuple[EvaluationCase, ...]

    @model_validator(mode="after")
    def _validate_coverage(self) -> EvaluationDataset:
        if len(self.cases) < _MINIMUM_CASES:
            raise ValueError("冻结集不得少于 50 题。")
        identifiers = [case.id for case in self.cases]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("冻结集含重复题号。")
        split_counts = Counter(case.split for case in self.cases)
        if split_counts["holdout"] < _MINIMUM_HOLDOUT:
            raise ValueError("holdout 不得少于 15 题。")
        categories = {
            category for case in self.cases for category in case.categories
        }
        missing = _REQUIRED_CATEGORIES - categories
        if missing:
            raise ValueError(f"冻结集缺少类别：{sorted(missing)}")
        ocr_count = sum("ocr" in case.categories for case in self.cases)
        if ocr_count < _MINIMUM_OCR_CASES:
            raise ValueError("冻结集至少需要 5 道 OCR 题。")
        return self


class HoldoutQuestion(BaseModel):
    """不暴露标签的 holdout 查询视图。"""

    model_config = ConfigDict(frozen=True)

    id: str
    categories: tuple[str, ...]
    history_questions: tuple[str, ...]
    question: str
    validation_state: str


def load_dataset(path: Path) -> EvaluationDataset:
    """读取完整冻结集，仅供校验器和显式验收使用。"""
    return EvaluationDataset.model_validate_json(path.read_text("utf-8"))


def load_tuning_cases(path: Path) -> tuple[EvaluationCase, ...]:
    """只向调参代码返回 tuning 标签。"""
    return tuple(
        case for case in load_dataset(path).cases if case.split == "tuning"
    )


def load_holdout_questions(path: Path) -> tuple[HoldoutQuestion, ...]:
    """返回不含 expected 标签的 holdout 问题。"""
    return tuple(
        HoldoutQuestion(
            id=case.id,
            categories=case.categories,
            history_questions=case.history_questions,
            question=case.question,
            validation_state=case.validation_state,
        )
        for case in load_dataset(path).cases
        if case.split == "holdout"
    )


def verify_source_evidence(
    dataset: EvaluationDataset,
    docs_root: Path,
) -> dict[str, int]:
    """逐条验证 locator 与逐字 quote 确实存在于冻结 DOCX。

    Args:
        dataset: 已通过结构校验的冻结集。
        docs_root: 只读 DOCX 根目录。

    Returns:
        题目、文本证据和 OCR locator 的核验计数。

    Raises:
        ValueError: 文档键、locator 或 quote 不能匹配。

    """
    parser = DocxParser()
    parsed: dict[str, list[tuple[str, str]]] = {}
    text_evidence = 0
    ocr_locators = 0
    for case in dataset.cases:
        for evidence in case.expected.evidence:
            relative_path = dataset.documents.get(evidence.document)
            if relative_path is None:
                raise ValueError(f"{case.id}: 未知文档键。")
            if evidence.document not in parsed:
                elements = parser.parse(
                    docs_root / relative_path,
                    display_path=relative_path,
                )
                parsed[evidence.document] = [
                    (element.locator.display(), element.text)
                    for element in elements
                ]
            matches = [
                text
                for locator, text in parsed[evidence.document]
                if evidence.locator_contains in locator
            ]
            if not matches:
                raise ValueError(f"{case.id}: locator 未命中。")
            if evidence.quote is None:
                ocr_locators += 1
            elif not any(evidence.quote in text for text in matches):
                raise ValueError(f"{case.id}: quote 未命中 locator 原文。")
            else:
                text_evidence += 1
    return {
        "cases": len(dataset.cases),
        "holdout": sum(case.split == "holdout" for case in dataset.cases),
        "text_evidence": text_evidence,
        "ocr_locators": ocr_locators,
    }
