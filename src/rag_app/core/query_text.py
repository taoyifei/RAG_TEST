"""查询文本的基础规范化规则。"""

from __future__ import annotations

import re
import unicodedata


def normalize_identifier(identifier: str) -> str:
    """生成 NFKC、casefold 与分隔符统一后的 identifier。

    Args:
        identifier: 原始 identifier。

    Returns:
        用单个连字符连接的规范形式。

    """
    normalized = unicodedata.normalize("NFKC", identifier).casefold().strip()
    return re.sub(r"[\s_./\\-]+", "-", normalized).strip("-")


__all__ = ["normalize_identifier"]
