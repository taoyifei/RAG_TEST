"""根据真实引用与检索事件标注回答来源，不推断或改写回答内容。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

AnswerOrigin = Literal[
    "knowledge", "model_no_results", "model_unreferenced", "unknown"
]


class AnswerOriginTracker:
    """记录本轮检索证据，保守地区分零结果和未附引用。"""

    def __init__(self) -> None:
        self._found_results = False
        self._zero_results = False

    def accept(self, payload: Mapping[str, Any]) -> None:
        """只采信成功的知识检索工具计数，不把调用或错误当作零结果。"""
        if payload.get("response_type") != "tool_result":
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        if (
            data.get("tool_name") != "knowledge_search"
            or data.get("success") is not True
        ):
            return
        count = data.get("count")
        nested = data.get("data")
        if count is None and isinstance(nested, dict):
            count = nested.get("count")
        # bool 是 Python int 的子类，但不是有效的检索计数。
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return
        self._found_results |= count > 0
        self._zero_results |= count == 0

    def restore(self, events: Iterable[Mapping[str, Any]]) -> None:
        """续流及历史均从同一轮已持久化的原生事件恢复证据。"""
        for event in events:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            native = payload.get("native")
            if isinstance(native, Mapping):
                self.accept(native)

    def origin(
        self, *, has_citations: bool, has_answer: bool, completed: bool
    ) -> AnswerOrigin:
        """引用优先；任一次真实命中都阻止“未找到资料”的误标。"""
        if has_citations:
            return "knowledge"
        if not has_answer:
            return "unknown"
        if self._zero_results and not self._found_results:
            return "model_no_results"
        # 未结束且未观察到零结果时，引用仍可能在正文之后到达。
        return "model_unreferenced" if completed else "unknown"


def answer_origin_from_events(
    events: Iterable[Mapping[str, Any]],
    *,
    citations: Sequence[Mapping[str, Any]],
    status: str,
    answer: str | None,
) -> AnswerOrigin:
    """从本轮已持久化事件恢复来源，历史与实时共用同一判定规则。

    Args:
        events: 网关记录的事件，正文在 payload 下。
        citations: 该轮当前可见的引用。
        status: 已核对原生消息状态的本轮状态。
        answer: 可展示的回答正文。

    Returns:
        来源标识。失败、取消或等待确认时不声明模型已完成回答。

    """
    if status != "completed":
        return "unknown"
    tracker = AnswerOriginTracker()
    tracker.restore(events)
    return tracker.origin(
        has_citations=bool(citations),
        has_answer=bool(answer and answer.strip()),
        completed=True,
    )
