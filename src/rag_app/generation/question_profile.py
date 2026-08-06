"""问题的多轴语义描述与高精度结构信号。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from rag_app.generation.question_intent import (
    QuestionIntent,
    classify_question_intent,
)
from rag_app.tracing.models import JsonValue

__all__ = [
    "PrimaryOperation",
    "QuestionProfile",
    "RequestedSlot",
    "RouteSource",
    "StructuralSignals",
    "extract_structural_signals",
    "legacy_question_profile",
]

_SLOT_ORDER = (
    "ACTOR",
    "DELIVERABLE",
    "CONDITION",
    "TIME",
    "INPUT",
    "OUTPUT",
    "STANDARD",
    "SCOPE",
)
_MAX_SECONDARY_OPERATIONS = 2
_ANCHOR_PATTERNS = (
    re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)"),
    re.compile(r"(?<!\d)\d+(?:\.\d+)?%(?!\d)"),
    re.compile(r"(?:v|V|版本)\s*\d+(?:\.\d+){0,3}"),
    re.compile(r"(?:第\s*\d+\s*条|\d+(?:\.\d+)+\s*条)"),
    re.compile(r"[A-Z]{2,}[\-－]\d+(?:\.\d+)*"),
    re.compile(r"《[^》\n]{1,80}》"),
)
_BINARY_CHOICE = re.compile(
    r"(?P<left>[^，。！？?；;]{1,80}?)\s*还是\s*"
    r"(?P<right>[^，。！？?；;]{1,80})"
)
_NON_CHOICE_LEFT_SIDES = frozenset({"为什么", "怎么", "如何", "怎样"})
_SLOT_MARKERS: tuple[tuple[RequestedSlot, tuple[str, ...]], ...]


class PrimaryOperation(StrEnum):
    """回答应采用的主要组织方式。"""

    DEFINITION = "DEFINITION"
    PROCEDURE = "PROCEDURE"
    LIST = "LIST"
    COMPARE = "COMPARE"
    DECISION = "DECISION"
    EXPLANATION = "EXPLANATION"
    GENERAL = "GENERAL"


class RequestedSlot(StrEnum):
    """用户明确要求覆盖的高精度信息槽位。"""

    ACTOR = "ACTOR"
    DELIVERABLE = "DELIVERABLE"
    CONDITION = "CONDITION"
    TIME = "TIME"
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"
    STANDARD = "STANDARD"
    SCOPE = "SCOPE"


class RouteSource(StrEnum):
    """产生当前 profile 的路由来源。"""

    LEGACY = "LEGACY"
    STRUCTURAL = "STRUCTURAL"
    SEMANTIC = "SEMANTIC"
    LLM = "LLM"
    GENERAL = "GENERAL"


_SLOT_MARKERS = (
    (RequestedSlot.ACTOR, ("由谁", "谁负责", "责任人", "负责人是谁")),
    (
        RequestedSlot.DELIVERABLE,
        ("输出什么报告", "什么文档", "交付物是什么"),
    ),
    (RequestedSlot.INPUT, ("需要哪些输入", "输入材料")),
    (RequestedSlot.OUTPUT, ("输出结果", "最终产出")),
    (RequestedSlot.CONDITION, ("适用条件", "前提条件", "什么情况下")),
    (RequestedSlot.STANDARD, ("验收标准", "判断标准", "依据什么规范")),
    (RequestedSlot.TIME, ("什么时间", "哪个阶段", "多长时间")),
    (RequestedSlot.SCOPE, ("适用范围", "覆盖范围", "边界是什么")),
)


@dataclass(frozen=True, slots=True)
class StructuralSignals:
    """不承担主分类职责的结构化查询信号。"""

    requested_slots: tuple[RequestedSlot, ...]
    anchor_count: int
    binary_choice_candidate: bool
    binary_choice_left: str | None = None
    binary_choice_right: str | None = None

    def __post_init__(self) -> None:
        """校验槽位、锚点和二选一内部信息的一致性。"""
        if self.anchor_count < 0:
            raise ValueError("anchor_count 不能为负数。")
        if len(set(self.requested_slots)) != len(self.requested_slots):
            raise ValueError("requested_slots 不能重复。")
        if tuple(sorted(self.requested_slots, key=_slot_position)) != (
            self.requested_slots
        ):
            raise ValueError("requested_slots 顺序必须稳定。")
        sides = (self.binary_choice_left, self.binary_choice_right)
        has_both_sides = all(side is not None for side in sides)
        if self.binary_choice_candidate != has_both_sides:
            raise ValueError("二选一标记与两侧文本必须一致。")


@dataclass(frozen=True, slots=True)
class QuestionProfile:
    """传给回答层的不可变多轴问题描述。"""

    primary_operation: PrimaryOperation
    secondary_operations: tuple[PrimaryOperation, ...]
    requested_slots: tuple[RequestedSlot, ...]
    confidence: float
    margin: float
    route_source: RouteSource
    scores: tuple[tuple[PrimaryOperation, float], ...]
    fallback_used: bool
    reason_code: str

    def __post_init__(self) -> None:
        """拒绝越界分数、重复辅助操作和不稳定顺序。"""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 必须在 [0, 1] 内。")
        if not 0.0 <= self.margin <= 1.0:
            raise ValueError("margin 必须在 [0, 1] 内。")
        if len(self.secondary_operations) > _MAX_SECONDARY_OPERATIONS:
            raise ValueError("secondary_operations 最多两个。")
        if self.primary_operation in self.secondary_operations:
            raise ValueError(
                "primary_operation 不得出现在 secondary_operations。"
            )
        if PrimaryOperation.GENERAL in self.secondary_operations:
            raise ValueError("GENERAL 不得作为 secondary_operations。")
        if len(set(self.secondary_operations)) != len(
            self.secondary_operations
        ):
            raise ValueError("secondary_operations 不能重复。")
        if len(set(self.requested_slots)) != len(self.requested_slots):
            raise ValueError("requested_slots 不能重复。")
        if tuple(sorted(self.requested_slots, key=_slot_position)) != (
            self.requested_slots
        ):
            raise ValueError("requested_slots 顺序必须稳定。")
        score_operations = tuple(operation for operation, _ in self.scores)
        if len(set(score_operations)) != len(score_operations):
            raise ValueError("scores 的 operation 不能重复。")
        if any(not 0.0 <= score <= 1.0 for _, score in self.scores):
            raise ValueError("scores 必须在 [0, 1] 内。")
        if any(
            earlier < later
            for (_, earlier), (_, later) in zip(
                self.scores,
                self.scores[1:],
                strict=False,
            )
        ):
            raise ValueError("scores 必须按分数降序。")
        if not self.reason_code.strip():
            raise ValueError("reason_code 不能为空。")

    def as_prompt_payload(self) -> dict[str, JsonValue]:
        """返回回答模型可见的最小组织提示。

        Args:
            无参数；仅读取当前不可变 profile。

        Returns:
            不含分数、原因或其它内部诊断的 prompt JSON。

        """
        return {
            "primary_operation": self.primary_operation.value,
            "secondary_operations": [
                operation.value for operation in self.secondary_operations
            ],
            "requested_slots": [slot.value for slot in self.requested_slots],
        }


def extract_structural_signals(resolved_query: str) -> StructuralSignals:
    """从独立查询提取确定性槽位、锚点和二选一候选。

    Args:
        resolved_query: 已由改写阶段确定且不在此处修改的查询。

    Returns:
        不含主 operation 判断的结构信号。

    Raises:
        ValueError: 查询去除首尾空白后为空。

    """
    query = resolved_query.strip()
    if not query:
        raise ValueError("resolved_query 不能为空。")
    slots = tuple(
        slot
        for slot in RequestedSlot
        if any(
            marker in query
            for current_slot, markers in _SLOT_MARKERS
            if current_slot is slot
            for marker in markers
        )
    )
    choice = _BINARY_CHOICE.search(query)
    if (
        choice is not None
        and choice.group("left").strip() in _NON_CHOICE_LEFT_SIDES
    ):
        choice = None
    return StructuralSignals(
        requested_slots=slots,
        anchor_count=sum(
            len(pattern.findall(query)) for pattern in _ANCHOR_PATTERNS
        ),
        binary_choice_candidate=choice is not None,
        binary_choice_left=(
            None if choice is None else choice.group("left").strip()
        ),
        binary_choice_right=(
            None if choice is None else choice.group("right").strip()
        ),
    )


def legacy_question_profile(question: str) -> QuestionProfile:
    """将旧词典分类映射为兼容的多轴 profile。

    Args:
        question: 当前回答问题；旧分类器继续仅用于回滚和 shadow 对照。

    Returns:
        使用 legacy 路由来源的稳定 profile。

    """
    intent = classify_question_intent(question)
    operation, slots = _legacy_mapping(intent)
    return QuestionProfile(
        primary_operation=operation,
        secondary_operations=(),
        requested_slots=slots,
        confidence=1.0,
        margin=1.0,
        route_source=RouteSource.LEGACY,
        scores=((operation, 1.0),),
        fallback_used=False,
        reason_code="LEGACY_CLASSIFIER",
    )


def _legacy_mapping(
    intent: QuestionIntent,
) -> tuple[PrimaryOperation, tuple[RequestedSlot, ...]]:
    if intent is QuestionIntent.ACTOR:
        return PrimaryOperation.LIST, (RequestedSlot.ACTOR,)
    if intent is QuestionIntent.DELIVERABLE:
        return PrimaryOperation.LIST, (RequestedSlot.DELIVERABLE,)
    return PrimaryOperation(intent.value), ()


def _slot_position(slot: RequestedSlot) -> int:
    return _SLOT_ORDER.index(slot.value)
