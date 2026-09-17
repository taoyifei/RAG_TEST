"""单次兼容解析只接受完整且唯一的结构化对象。"""

from __future__ import annotations

import pytest

from rag_app.product.structured_json import extract_json_object


@pytest.mark.parametrize(
    "content",
    [
        '{"ok":true}',
        '\ufeff <think>虚构推理</think>\n{"ok":true}',
        '```json\n{"ok":true}\n```',
        '<think>虚构推理</think>\n```json\n{"ok":true}\n```',
        '{"text":"内含 \\"} 和 { 字符"}',
    ],
)
def test_extracts_one_balanced_object(content: str) -> None:
    assert isinstance(extract_json_object(content), dict)


@pytest.mark.parametrize(
    "content",
    [
        '以下是答案：{"ok":true}',
        '{"ok":true}{"ok":false}',
        '{"ok":true} 业务解释',
        '<think>未闭合{"ok":true}',
        '```json\n{"ok":true}',
        '{"ok":true',
    ],
)
def test_rejects_business_prose_multiple_objects_or_incomplete_json(
    content: str,
) -> None:
    with pytest.raises(ValueError):
        extract_json_object(content)
