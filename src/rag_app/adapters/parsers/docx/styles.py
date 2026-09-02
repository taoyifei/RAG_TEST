"""解析 DOCX 段落样式及可证明的继承属性。"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.models import EffectiveStyle
from rag_app.adapters.parsers.docx.namespaces import WORD, qn, word_attr
from rag_app.adapters.parsers.docx.package import DocxPackage
from rag_app.core.policies import ParsingPolicy

_MAX_OUTLINE_LEVEL = 8
_MAX_HEADING_LEVEL = 9


@dataclass(frozen=True, slots=True)
class _StyleDefinition:
    style_id: str
    name: str | None
    based_on: str | None
    outline_level: int | None
    num_id: int | None
    num_level: int | None
    hidden: bool | None
    next_style_id: str | None
    linked_style_id: str | None
    quick_format: bool
    unhide_when_used: bool


class StyleCatalog:
    """段落样式定义和继承解析器。"""

    def __init__(
        self,
        definitions: dict[str, _StyleDefinition],
        issues: IssueCollector,
    ) -> None:
        """保存样式定义并建立延迟缓存。

        Args:
            definitions: 按 style ID 索引的段落样式。
            issues: 共享安全问题收集器。

        Returns:
            无返回值。

        """
        self._definitions = definitions
        self._issues = issues
        self._cache: dict[str, EffectiveStyle] = {}

    @classmethod
    def from_package(
        cls,
        package: DocxPackage,
        issues: IssueCollector,
    ) -> StyleCatalog:
        """从 styles Part 创建 catalog。

        Args:
            package: 已校验的 DOCX package。
            issues: 共享问题收集器。

        Returns:
            允许空 styles Part 的样式 catalog。

        """
        part_uri = _related_part(package, "styles")
        if part_uri is None:
            issues.add(
                "DOCX_STYLES_PART_MISSING",
                action="defaults_only",
                message="文档未提供 styles Part，未推断样式语义。",
            )
            return cls({}, issues)
        root = package.xml(part_uri)
        definitions: dict[str, _StyleDefinition] = {}
        for style in root.findall(qn(WORD, "style")):
            if style.get(word_attr("type")) != "paragraph":
                continue
            style_id = style.get(word_attr("styleId"))
            if not style_id:
                continue
            name_node = style.find(qn(WORD, "name"))
            based_on_node = style.find(qn(WORD, "basedOn"))
            next_node = style.find(qn(WORD, "next"))
            link_node = style.find(qn(WORD, "link"))
            properties = style.find(qn(WORD, "pPr"))
            outline_level = _integer_child(properties, "outlineLvl")
            num_id, num_level = _numbering_properties(properties)
            hidden = (
                True
                if style.find(qn(WORD, "semiHidden")) is not None
                else None
            )
            definitions[style_id] = _StyleDefinition(
                style_id=style_id,
                name=(
                    None
                    if name_node is None
                    else name_node.get(word_attr("val"))
                ),
                based_on=(
                    None
                    if based_on_node is None
                    else based_on_node.get(word_attr("val"))
                ),
                outline_level=outline_level,
                num_id=num_id,
                num_level=num_level,
                hidden=hidden,
                next_style_id=(
                    None
                    if next_node is None
                    else next_node.get(word_attr("val"))
                ),
                linked_style_id=(
                    None
                    if link_node is None
                    else link_node.get(word_attr("val"))
                ),
                quick_format=style.find(qn(WORD, "qFormat")) is not None,
                unhide_when_used=(
                    style.find(qn(WORD, "unhideWhenUsed")) is not None
                ),
            )
        return cls(definitions, issues)

    def effective(self, style_id: str | None) -> EffectiveStyle | None:
        """计算一个段落样式的有效属性。

        Args:
            style_id: 段落直接引用的 style ID。

        Returns:
            可证明属性；样式缺失时为 `None`。

        """
        if style_id is None:
            return None
        cached = self._cache.get(style_id)
        if cached is not None:
            return cached
        definition = self._definitions.get(style_id)
        if definition is None:
            self._issues.add(
                "DOCX_STYLE_MISSING",
                action="direct_properties_only",
                message="段落引用的样式不存在。",
                metadata=(("style_id", style_id),),
            )
            return None
        chain: list[_StyleDefinition] = []
        visited: set[str] = set()
        current: _StyleDefinition | None = definition
        while current is not None:
            if current.style_id in visited:
                self._issues.add(
                    "DOCX_STYLE_INHERITANCE_CYCLE",
                    action="stop_at_cycle",
                    message="样式 basedOn 链存在环。",
                    metadata=(("style_id", style_id),),
                )
                break
            visited.add(current.style_id)
            chain.append(current)
            if current.based_on is None:
                break
            parent = self._definitions.get(current.based_on)
            if parent is None:
                self._issues.add(
                    "DOCX_STYLE_PARENT_MISSING",
                    action="stop_at_missing_parent",
                    message="样式 basedOn 父样式不存在。",
                    metadata=(("style_id", current.style_id),),
                )
                break
            current = parent
        name: str | None = None
        outline_level: int | None = None
        num_id: int | None = None
        num_level: int | None = None
        hidden = False
        for item in reversed(chain):
            name = item.name if item.name is not None else name
            outline_level = (
                item.outline_level
                if item.outline_level is not None
                else outline_level
            )
            num_id = item.num_id if item.num_id is not None else num_id
            num_level = (
                item.num_level if item.num_level is not None else num_level
            )
            hidden = item.hidden if item.hidden is not None else hidden
        effective = EffectiveStyle(
            style_id=style_id,
            name=name,
            outline_level=outline_level,
            num_id=num_id,
            num_level=num_level,
            hidden=hidden,
            next_style_id=definition.next_style_id,
            linked_style_id=definition.linked_style_id,
            quick_format=definition.quick_format,
            unhide_when_used=definition.unhide_when_used,
        )
        self._cache[style_id] = effective
        return effective

    def heading_level(
        self,
        paragraph: etree._Element,
        policy: ParsingPolicy,
    ) -> int | None:
        """按固定优先级识别一到九级标题。

        Args:
            paragraph: 待判断的 Word 段落。
            policy: 包含显式自定义标题样式映射的策略。

        Returns:
            一到九级标题，否则为 `None`。

        """
        properties = paragraph.find(qn(WORD, "pPr"))
        direct_outline = _integer_child(properties, "outlineLvl")
        style_id = _style_id(properties)
        effective = self.effective(style_id)
        outline = (
            direct_outline
            if direct_outline is not None
            else effective.outline_level
            if effective is not None
            else None
        )
        if outline is not None and 0 <= outline <= _MAX_OUTLINE_LEVEL:
            return outline + 1
        candidates = [style_id or ""]
        if effective is not None and effective.name:
            candidates.append(effective.name)
        for candidate in candidates:
            level = _standard_heading_level(candidate)
            if level is not None:
                return level
        custom = {
            name.casefold(): int(level)
            for name, level in policy.custom_heading_styles
        }
        for candidate in candidates:
            level = custom.get(candidate.casefold())
            if level is not None:
                return level
        return None

    def numbering(
        self,
        paragraph: etree._Element,
    ) -> tuple[int | None, int | None]:
        """返回直接属性优先的有效 numId 和 ilvl。

        Args:
            paragraph: 待解析的 Word 段落。

        Returns:
            `(num_id, level)`；普通段落均为 `None`。

        """
        properties = paragraph.find(qn(WORD, "pPr"))
        direct_id, direct_level = _numbering_properties(properties)
        effective = self.effective(_style_id(properties))
        num_id = (
            direct_id
            if direct_id is not None
            else effective.num_id
            if effective is not None
            else None
        )
        level = (
            direct_level
            if direct_level is not None
            else effective.num_level
            if effective is not None
            else None
        )
        if num_id == 0:
            return None, None
        return num_id, 0 if num_id is not None and level is None else level


def _related_part(package: DocxPackage, suffix: str) -> str | None:
    for relationship in package.catalog.relationships_from(
        package.catalog.main_part_uri
    ):
        if relationship.relationship_type.endswith(f"/{suffix}"):
            return relationship.target_part_uri
    fallback = f"/word/{suffix}.xml"
    return fallback if package.catalog.part(fallback) is not None else None


def _style_id(properties: etree._Element | None) -> str | None:
    if properties is None:
        return None
    node = properties.find(qn(WORD, "pStyle"))
    return None if node is None else node.get(word_attr("val"))


def _integer_child(
    parent: etree._Element | None,
    local_name: str,
) -> int | None:
    if parent is None:
        return None
    child = parent.find(qn(WORD, local_name))
    if child is None:
        return None
    value = child.get(word_attr("val"))
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _numbering_properties(
    properties: etree._Element | None,
) -> tuple[int | None, int | None]:
    if properties is None:
        return None, None
    numbering = properties.find(qn(WORD, "numPr"))
    if numbering is None:
        return None, None
    return (
        _integer_child(numbering, "numId"),
        _integer_child(numbering, "ilvl"),
    )


def _standard_heading_level(value: str) -> int | None:
    normalized = "".join(value.casefold().split())
    for prefix in ("heading", "标题"):
        if not normalized.startswith(prefix):
            continue
        suffix = normalized.removeprefix(prefix)
        if suffix.isdigit() and 1 <= int(suffix) <= _MAX_HEADING_LEVEL:
            return int(suffix)
    return None
