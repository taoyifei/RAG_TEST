"""湾事通未来元数据筛选与快捷入口的服务端脚手架。"""

from __future__ import annotations

from pydantic import Field, model_validator

from rag_app.core.models.common import FrozenModel


class MetadataFilterExpression(FrozenModel):
    """由可信服务端定义生成的类型化文档元数据条件。"""

    department_keys: tuple[str, ...] = Field(default=(), max_length=32)
    category_prefixes: tuple[tuple[str, ...], ...] = Field(
        default=(), max_length=32
    )
    topic_keys: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _validate_unique_values(self) -> MetadataFilterExpression:
        if len(self.department_keys) != len(set(self.department_keys)):
            raise ValueError("department_keys 禁止重复。")
        if len(self.category_prefixes) != len(set(self.category_prefixes)):
            raise ValueError("category_prefixes 禁止重复。")
        if len(self.topic_keys) != len(set(self.topic_keys)):
            raise ValueError("topic_keys 禁止重复。")
        if any(not prefix for prefix in self.category_prefixes):
            raise ValueError("category prefix 禁止为空。")
        return self


class PublicMetadataFilter(MetadataFilterExpression):
    """未来公共请求的筛选形状；WB-06 只允许默认空值。"""

    shortcut_id: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"
    )

    @property
    def is_empty(self) -> bool:
        """返回当前请求是否保持全量检索。"""
        return not (
            self.department_keys
            or self.category_prefixes
            or self.topic_keys
            or self.shortcut_id
        )


class ShortcutDefinition(FrozenModel):
    """只能由服务端可信代码注册的快捷入口定义。"""

    shortcut_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    enabled: bool = False
    visible: bool = False
    sort_order: int = Field(ge=0, le=10000)
    filter_expression: MetadataFilterExpression
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _visible_requires_enabled(self) -> ShortcutDefinition:
        if self.visible and not self.enabled:
            raise ValueError("可见快捷入口必须先启用。")
        return self


class ShortcutCatalog:
    """把稳定 ID 解析为服务端定义，不接受客户端表达式。"""

    def __init__(self, definitions: tuple[ShortcutDefinition, ...]) -> None:
        if len({item.shortcut_id for item in definitions}) != len(definitions):
            raise ValueError("shortcut_id 禁止重复。")
        self._definitions = tuple(
            sorted(
                definitions,
                key=lambda item: (item.sort_order, item.shortcut_id),
            )
        )
        self._by_id = {item.shortcut_id: item for item in self._definitions}

    def public_definitions(self) -> tuple[ShortcutDefinition, ...]:
        """仅返回同时启用且可见的公共定义。"""
        return tuple(
            item for item in self._definitions if item.enabled and item.visible
        )

    def resolve_filter(self, shortcut_id: str) -> MetadataFilterExpression:
        """把可信 Shortcut ID 转换为类型化筛选表达式。"""
        definition = self._by_id.get(shortcut_id)
        if definition is None or not definition.enabled:
            raise KeyError(shortcut_id)
        return definition.filter_expression


DRAFT_SHORTCUTS = (
    ShortcutDefinition(
        shortcut_id="policy",
        label="制度政策",
        description="按制度与政策主题检索。",
        sort_order=10,
        filter_expression=MetadataFilterExpression(topic_keys=("policy",)),
        revision=1,
    ),
    ShortcutDefinition(
        shortcut_id="process",
        label="流程规范",
        description="按流程与规范主题检索。",
        sort_order=20,
        filter_expression=MetadataFilterExpression(topic_keys=("process",)),
        revision=1,
    ),
    ShortcutDefinition(
        shortcut_id="hr-service",
        label="人事服务",
        description="按人事服务主题检索。",
        sort_order=30,
        filter_expression=MetadataFilterExpression(topic_keys=("hr-service",)),
        revision=1,
    ),
    ShortcutDefinition(
        shortcut_id="technical",
        label="技术资料",
        description="按技术资料主题检索。",
        sort_order=40,
        filter_expression=MetadataFilterExpression(topic_keys=("technical",)),
        revision=1,
    ),
)
SHORTCUT_CATALOG = ShortcutCatalog(DRAFT_SHORTCUTS)


def current_public_filter() -> PublicMetadataFilter:
    """返回 WB-06 强制使用的空筛选，保持默认全量召回。"""
    return PublicMetadataFilter()


__all__ = [
    "DRAFT_SHORTCUTS",
    "SHORTCUT_CATALOG",
    "MetadataFilterExpression",
    "PublicMetadataFilter",
    "ShortcutCatalog",
    "ShortcutDefinition",
    "current_public_filter",
]
