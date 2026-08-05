"""确定性识别证据回答所需的问题意图。"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["QuestionIntent", "classify_question_intent"]


class QuestionIntent(StrEnum):
    """回答 Prompt 使用的有限问题意图。"""

    PROCEDURE = "PROCEDURE"
    LIST = "LIST"
    DEFINITION = "DEFINITION"
    GENERAL = "GENERAL"


_DEFINITION_MARKERS = ("什么是", "含义", "定义")
_PROCEDURE_MARKERS = (
    "步骤",
    "流程",
    "审批",
    "如何",
    "怎么",
    "办理",
    "处理",
)
_LIST_MARKERS = ("哪些", "要求", "条件", "材料", "职责", "内容")


def classify_question_intent(question: str) -> QuestionIntent:
    """按固定关键词优先级识别问题意图。

    定义短语比通用流程词更具体；其余情况下流程词优先于列表词，确保
    “哪些审批步骤”归入 PROCEDURE。

    Args:
        question: 已确认非空的当前问题。

    Returns:
        确定性问题意图。

    Raises:
        ValueError: 问题去除首尾空白后为空。

    """
    normalized = question.strip()
    if not normalized:
        raise ValueError("question 不能为空。")
    if any(marker in normalized for marker in _DEFINITION_MARKERS):
        return QuestionIntent.DEFINITION
    if any(marker in normalized for marker in _PROCEDURE_MARKERS):
        return QuestionIntent.PROCEDURE
    if any(marker in normalized for marker in _LIST_MARKERS):
        return QuestionIntent.LIST
    return QuestionIntent.GENERAL
