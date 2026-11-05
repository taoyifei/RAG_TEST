"""按回答实际引用标注来源，不把检索候选当作回答依据。"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

AnswerOrigin = Literal[
    "knowledge",
    "model_no_results",
    "model_unreferenced",
    "unknown",
]

_KB_TAG = re.compile(r"<kb\b([^<>]*?)/?>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"""([\w-]+)\s*=\s*(["'])(.*?)\2""")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+).*?\1")
_RESOURCE_URL = re.compile(r"""(?:resource|https?)://[^\s<>"'\\)\]]+""")
_IMAGE = re.compile(
    r"""!\[[^\]\n]*\]\(\s*<?((?:resource|https?)://[^\s<>)]*)>?"""
    r"""(?:\s+["'][^"']*["'])?\s*\)"""
)


def _reference_chunk_ids(reference: Mapping[str, Any]) -> set[str]:
    identifiers = (
        {str(reference["native_chunk_id"])}
        if reference.get("native_chunk_id")
        else set()
    )
    native = reference.get("native_reference")
    children = (
        native.get("sub_chunk_id") if isinstance(native, Mapping) else None
    )
    if isinstance(children, list):
        identifiers.update(
            item for item in children if isinstance(item, str) and item
        )
    return identifiers


def answer_cites_references(
    answer: str, references: Sequence[Mapping[str, Any]]
) -> bool:
    """只接受正文中指向本轮检索片段的原生引用，排除代码示例。"""
    available = {
        chunk_id: item
        for item in references
        for chunk_id in _reference_chunk_ids(item)
    }
    fence = ""
    for line in answer.splitlines():
        marker = _FENCE.match(line)
        if marker:
            current = marker[1]
            if not fence:
                fence = current
            elif current[0] == fence[0] and len(current) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        text = _INLINE_CODE.sub("", line)
        images = {match[1] for match in _IMAGE.finditer(text)}
        if images:
            for reference in references:
                native = reference.get("native_reference")
                image_info = (
                    native.get("image_info")
                    if isinstance(native, Mapping)
                    else None
                )
                sources = (
                    str(reference.get("quote", ""))
                    + "\n"
                    + json.dumps(image_info)
                )
                if images.intersection(_RESOURCE_URL.findall(sources)):
                    return True
        for tag in _KB_TAG.finditer(text):
            attributes = {
                match[1]: html.unescape(match[3])
                for match in _ATTRIBUTE.finditer(tag[1])
            }
            chunk = attributes.get("chunk_id", attributes.get("chunkId", ""))
            cited_reference = available.get(chunk)
            if cited_reference is None:
                continue
            supplied_kb = attributes.get("kb_id")
            native = cited_reference.get("native_reference")
            expected_kb = (
                native.get("knowledge_base_id")
                if isinstance(native, Mapping)
                else None
            )
            if supplied_kb and supplied_kb != expected_kb:
                continue
            return True
    return False


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
        self,
        *,
        citations: Sequence[Mapping[str, Any]],
        answer: str,
        completed: bool,
    ) -> AnswerOrigin:
        """只核对本轮实际引用；不改写正文，也不追加模型判断。"""
        if not answer.strip():
            return "unknown"
        if answer_cites_references(answer, citations):
            return "knowledge"
        if self._zero_results and not self._found_results and not citations:
            return "model_no_results"
        if not completed:
            return "unknown"
        return "model_unreferenced"


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
        citations=citations,
        answer=answer or "",
        completed=True,
    )
