"""无受约束输出协议时，确定性提取唯一 JSON Object。"""

from __future__ import annotations

import json


def extract_json_object(content: str) -> dict[str, object]:
    """只接受空白、完整 leading think 或完整代码围栏之外的单个对象。"""
    text = _strip_wrappers(content)
    if not text.startswith("{"):
        raise ValueError("JSON 对象前存在无法解释的正文。")
    end_index = _balanced_end(text)
    if text[end_index:].strip():
        raise ValueError("JSON 对象后存在额外正文。")
    parsed = json.loads(text[:end_index])
    if not isinstance(parsed, dict):
        raise ValueError("顶层 JSON 必须为对象。")
    return parsed


def _strip_wrappers(content: str) -> str:
    """只移除完整且位于对象之前的受控包装。"""
    text = content.lstrip("\ufeff \t\r\n")
    if text.startswith("<think>"):
        end = text.find("</think>", len("<think>"))
        if end < 0:
            raise ValueError("未闭合的 thinking 区块。")
        text = text[end + len("</think>") :].strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline < 0 or text[:newline].strip() not in {"```", "```json"}:
            raise ValueError("JSON 围栏格式无效。")
        if not text.rstrip().endswith("```"):
            raise ValueError("JSON 围栏未闭合。")
        text = text[newline + 1 :].rstrip()
        text = text[: -len("```")].strip()
    return text


def _balanced_end(text: str) -> int:
    """识别 JSON 字符串转义，返回唯一顶层对象的结束位置。"""
    depth = 0
    in_string = False
    escaped = False
    end_index: int | None = None
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end_index = index + 1
                break
            if depth < 0:
                raise ValueError("JSON 对象边界无效。")
    if end_index is None or in_string:
        raise ValueError("JSON 对象不完整。")
    return end_index


__all__ = ["extract_json_object"]
