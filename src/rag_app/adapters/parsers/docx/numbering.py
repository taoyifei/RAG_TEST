"""解析 DOCX 自动编号并维护独立列表计数状态。"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.models import NumberingLabel
from rag_app.adapters.parsers.docx.namespaces import WORD, qn, word_attr
from rag_app.adapters.parsers.docx.package import DocxPackage

_MAX_ROMAN = 3999
_SINGLE_DIGIT_MAX = 9
_TEEN_MIN = 10
_TEEN_MAX = 19
_TENS_MIN = 20
_DOUBLE_DIGIT_MAX = 99


@dataclass(frozen=True, slots=True)
class _LevelDefinition:
    level: int
    start: int
    number_format: str
    level_text: str
    suffix: str
    restart_after_level: int | None


@dataclass(frozen=True, slots=True)
class _NumberingInstance:
    num_id: int
    abstract_num_id: int
    start_overrides: dict[int, int]
    level_overrides: dict[int, _LevelDefinition]


@dataclass(slots=True)
class _CounterState:
    values: dict[int, int] = field(default_factory=dict)
    generation: int = 0


class NumberingCatalog:
    """编号定义和每个 numId 的确定性计数状态。"""

    def __init__(
        self,
        levels: dict[int, dict[int, _LevelDefinition]],
        instances: dict[int, _NumberingInstance],
        issues: IssueCollector,
    ) -> None:
        """保存编号定义。

        Args:
            levels: abstractNum ID 到层级定义。
            instances: numId 到实例及覆盖。
            issues: 共享问题收集器。

        Returns:
            无返回值。

        """
        self._levels = levels
        self._instances = instances
        self._issues = issues
        self._states: dict[int, _CounterState] = {}

    @classmethod
    def from_package(
        cls,
        package: DocxPackage,
        issues: IssueCollector,
    ) -> NumberingCatalog:
        """从 numbering Part 创建 catalog。

        Args:
            package: 已校验的 DOCX package。
            issues: 共享问题收集器。

        Returns:
            允许无编号 Part 的 catalog。

        """
        part_uri = _numbering_part(package)
        if part_uri is None:
            return cls({}, {}, issues)
        root = package.xml(part_uri)
        levels: dict[int, dict[int, _LevelDefinition]] = {}
        for abstract in root.findall(qn(WORD, "abstractNum")):
            abstract_id = _required_integer(abstract, "abstractNumId")
            if abstract_id is None:
                continue
            level_map: dict[int, _LevelDefinition] = {}
            for level_node in abstract.findall(qn(WORD, "lvl")):
                definition = _parse_level(level_node)
                if definition is not None:
                    level_map[definition.level] = definition
            levels[abstract_id] = level_map
        instances: dict[int, _NumberingInstance] = {}
        for instance_node in root.findall(qn(WORD, "num")):
            num_id = _required_integer(instance_node, "numId")
            abstract_node = instance_node.find(qn(WORD, "abstractNumId"))
            abstract_id = _value_integer(abstract_node)
            if num_id is None or abstract_id is None:
                continue
            starts: dict[int, int] = {}
            overrides: dict[int, _LevelDefinition] = {}
            for override in instance_node.findall(qn(WORD, "lvlOverride")):
                level = _required_integer(override, "ilvl")
                if level is None:
                    continue
                start_override = override.find(qn(WORD, "startOverride"))
                start = _value_integer(start_override)
                if start is not None:
                    starts[level] = start
                override_level_node = override.find(qn(WORD, "lvl"))
                if override_level_node is not None:
                    parsed = _parse_level(
                        override_level_node,
                        default_level=level,
                    )
                    if parsed is not None:
                        overrides[level] = parsed
            instances[num_id] = _NumberingInstance(
                num_id=num_id,
                abstract_num_id=abstract_id,
                start_overrides=starts,
                level_overrides=overrides,
            )
        return cls(levels, instances, issues)

    def next_label(self, num_id: int, level: int) -> NumberingLabel:
        """推进一个列表实例并生成显示标签。

        Args:
            num_id: 具体编号实例 ID。
            level: 零基多级列表层级。

        Returns:
            标签、序号和 restart group。

        """
        instance = self._instances.get(num_id)
        if instance is None:
            self._issues.add(
                "DOCX_NUMBERING_INSTANCE_MISSING",
                action="label_unavailable",
                message="段落引用的 numId 不存在。",
                metadata=(("num_id", num_id),),
            )
            return NumberingLabel(
                marker=None,
                ordinal=0,
                restart_group=f"num:{num_id}:missing",
                ordered=None,
            )
        definition = self._definition(instance, level)
        if definition is None:
            self._issues.add(
                "DOCX_NUMBERING_LEVEL_MISSING",
                action="label_unavailable",
                message="段落引用的编号层级不存在。",
                metadata=(("num_id", num_id), ("level", level)),
            )
            return NumberingLabel(
                marker=None,
                ordinal=0,
                restart_group=f"num:{num_id}:level:{level}:missing",
                ordered=None,
            )
        state = self._states.setdefault(num_id, _CounterState())
        if level in state.values:
            state.values[level] += 1
        else:
            state.values[level] = instance.start_overrides.get(
                level,
                definition.start,
            )
            state.generation += 1
        self._reset_deeper_levels(state, instance, level)
        marker = self._render(instance, definition, state.values)
        return NumberingLabel(
            marker=marker,
            ordinal=state.values[level],
            restart_group=f"num:{num_id}:generation:{state.generation}",
            ordered=(definition.number_format != "bullet"),
        )

    def _definition(
        self,
        instance: _NumberingInstance,
        level: int,
    ) -> _LevelDefinition | None:
        return instance.level_overrides.get(level) or self._levels.get(
            instance.abstract_num_id,
            {},
        ).get(level)

    def _reset_deeper_levels(
        self,
        state: _CounterState,
        instance: _NumberingInstance,
        changed_level: int,
    ) -> None:
        for deeper in tuple(state.values):
            if deeper <= changed_level:
                continue
            definition = self._definition(instance, deeper)
            if definition is None:
                state.values.pop(deeper, None)
                continue
            restart_after = definition.restart_after_level
            if restart_after is None or changed_level <= restart_after:
                state.values.pop(deeper, None)

    def _render(
        self,
        instance: _NumberingInstance,
        definition: _LevelDefinition,
        values: dict[int, int],
    ) -> str | None:
        rendered = definition.level_text
        for placeholder_level in range(9):
            placeholder = f"%{placeholder_level + 1}"
            if placeholder not in rendered:
                continue
            value = values.get(placeholder_level)
            referenced = self._definition(instance, placeholder_level)
            replacement: str | None
            if value is None or referenced is None:
                replacement = ""
            else:
                replacement = _format_number(
                    value,
                    referenced.number_format,
                )
                if replacement is None:
                    self._issues.add(
                        "DOCX_NUMBER_FORMAT_UNSUPPORTED",
                        action="label_unavailable",
                        message="编号格式无法可靠生成标签。",
                        metadata=(
                            ("number_format", referenced.number_format),
                        ),
                    )
                    return None
            rendered = rendered.replace(placeholder, replacement)
        suffix = {"tab": "\t", "space": " ", "nothing": ""}.get(
            definition.suffix,
            "",
        )
        return f"{rendered}{suffix}"


def _numbering_part(package: DocxPackage) -> str | None:
    for relationship in package.catalog.relationships_from(
        package.catalog.main_part_uri
    ):
        if relationship.relationship_type.endswith("/numbering"):
            return relationship.target_part_uri
    fallback = "/word/numbering.xml"
    return fallback if package.catalog.part(fallback) is not None else None


def _parse_level(
    node: etree._Element,
    *,
    default_level: int | None = None,
) -> _LevelDefinition | None:
    level = _required_integer(node, "ilvl")
    if level is None:
        level = default_level
    if level is None:
        return None
    start = _value_integer(node.find(qn(WORD, "start"))) or 1
    number_format = _value(node.find(qn(WORD, "numFmt"))) or "decimal"
    level_text = _value(node.find(qn(WORD, "lvlText"))) or f"%{level + 1}"
    suffix = _value(node.find(qn(WORD, "suff"))) or "tab"
    restart = _value_integer(node.find(qn(WORD, "lvlRestart")))
    return _LevelDefinition(
        level=level,
        start=start,
        number_format=number_format,
        level_text=level_text,
        suffix=suffix,
        restart_after_level=(None if restart is None else restart - 1),
    )


def _value(node: etree._Element | None) -> str | None:
    return None if node is None else node.get(word_attr("val"))


def _value_integer(node: etree._Element | None) -> int | None:
    value = _value(node)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _required_integer(node: etree._Element, local_name: str) -> int | None:
    value = node.get(word_attr(local_name))
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _format_number(  # noqa: PLR0911
    value: int,
    number_format: str,
) -> str | None:
    if number_format == "decimal":
        return str(value)
    if number_format == "decimalZero":
        return f"{value:02d}"
    if number_format == "lowerLetter":
        return _letters(value).lower()
    if number_format == "upperLetter":
        return _letters(value)
    if number_format == "lowerRoman":
        return _roman(value).lower()
    if number_format == "upperRoman":
        return _roman(value)
    if number_format == "bullet":
        return str(value)
    if number_format in {"chineseCounting", "chineseLegalSimplified"}:
        return _chinese_number(value)
    return None


def _letters(value: int) -> str:
    if value <= 0:
        return str(value)
    result = ""
    current = value
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _roman(value: int) -> str:
    if value <= 0 or value > _MAX_ROMAN:
        return str(value)
    pairs = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    result: list[str] = []
    current = value
    for amount, symbol in pairs:
        while current >= amount:
            result.append(symbol)
            current -= amount
    return "".join(result)


def _chinese_number(value: int) -> str | None:
    digits = "零一二三四五六七八九"
    if 0 <= value <= _SINGLE_DIGIT_MAX:
        return digits[value]
    if _TEEN_MIN <= value <= _TEEN_MAX:
        return f"十{digits[value % 10] if value % 10 else ''}"
    if _TENS_MIN <= value <= _DOUBLE_DIGIT_MAX:
        ones = digits[value % 10] if value % 10 else ""
        return f"{digits[value // 10]}十{ones}"
    return None
