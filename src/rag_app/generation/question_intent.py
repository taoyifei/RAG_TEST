"""确定性识别证据回答所需的问题意图。"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["QuestionIntent", "classify_question_intent"]


class QuestionIntent(StrEnum):
    """回答 Prompt 使用的有限问题意图。"""

    PROCEDURE = "PROCEDURE"
    LIST = "LIST"
    DEFINITION = "DEFINITION"
    ACTOR = "ACTOR"
    DELIVERABLE = "DELIVERABLE"
    COMPARE = "COMPARE"


_COMPARE_MARKERS = ("区别", "不同", "是否相同", "对比", "比较")
_DEFINITION_MARKERS = ("什么是", "是什么", "含义", "定义", "介绍一下")
_ACTOR_MARKERS = ("由谁", "谁负责", "责任人", "负责人")
_DELIVERABLE_MARKERS = (
    "输出什么",
    "什么文档",
    "什么报告",
    "交付物",
    "产出物",
)
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

    比较、定义、责任人和交付物短语优先于通用流程词；其余情况下流程词
    优先于列表词，确保“哪些审批步骤”归入 PROCEDURE。

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
    classifiers = (
        (QuestionIntent.COMPARE, _COMPARE_MARKERS),
        (QuestionIntent.DEFINITION, _DEFINITION_MARKERS),
        (QuestionIntent.ACTOR, _ACTOR_MARKERS),
        (QuestionIntent.DELIVERABLE, _DELIVERABLE_MARKERS),
        (QuestionIntent.PROCEDURE, _PROCEDURE_MARKERS),
        (QuestionIntent.LIST, _LIST_MARKERS),
    )
    for intent, markers in classifiers:
        if any(marker in normalized for marker in markers):
            return intent
    return QuestionIntent.LIST
