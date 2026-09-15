"""湾事通产品模式与固定 Scope 常量。"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal


class ProductMode(StrEnum):
    """Universal Product Runtime 支持的产品外壳模式。"""

    UNIVERSAL = "universal"
    WANSHITONG = "wanshitong"


WANSHITONG_SCOPE_KEY: Final[Literal["wanshitong-default-scope"]] = (
    "wanshitong-default-scope"
)
WANSHITONG_PROJECT_NAME: Final = "湾事通"
WANSHITONG_KNOWLEDGE_BASE_NAME: Final = "湾事通知识库"


__all__ = [
    "WANSHITONG_KNOWLEDGE_BASE_NAME",
    "WANSHITONG_PROJECT_NAME",
    "WANSHITONG_SCOPE_KEY",
    "ProductMode",
]
