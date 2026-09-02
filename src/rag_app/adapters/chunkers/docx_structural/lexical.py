"""独立 lexical 文本规范化和 identifier 提取。"""

from __future__ import annotations

import re
import unicodedata

_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Za-z]{1,10}/[A-Za-z]{1,10}\s*\d+(?:-\d+)?"
    r"|[A-Za-z]{1,12}-\d[A-Za-z0-9.-]*"
    r"|[A-Za-z]\d{4,}"
    r")(?![A-Za-z0-9])"
)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_PUNCTUATION_SPACING_PATTERN = re.compile(r"\s*([|,;:，；：。！？])\s*")


def lexical_view(text: str) -> tuple[str, tuple[str, ...]]:
    """生成 NFKC/casefold 文本并按首次出现顺序提取标识符。

    Args:
        text: citation 与受控结构上下文组成的文本。

    Returns:
        lexical_text 和 identifier tokens。

    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _PUNCTUATION_SPACING_PATTERN.sub(r"\1", normalized)
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized).strip()
    identifiers: list[str] = []
    for match in _IDENTIFIER_PATTERN.finditer(
        unicodedata.normalize("NFKC", text)
    ):
        identifier = _WHITESPACE_PATTERN.sub(" ", match.group(0)).strip()
        if identifier not in identifiers:
            identifiers.append(identifier)
    if identifiers:
        normalized = f"{normalized}\nidentifiers: {' '.join(identifiers)}"
    return normalized, tuple(identifiers)
