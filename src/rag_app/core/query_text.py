"""查询文本的基础规范化规则。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from html import unescape

_DOCUMENT_EXTENSION = re.compile(r"\.(?:docx?|pdf|txt|md)$")
_CATALOG_ORIGINAL_SUFFIX = re.compile(
    r"\(原件\s*\.(?:docx?|pdf|xls|xlsx)\)$", re.IGNORECASE
)
_STRUCTURAL_SEPARATOR = re.compile(r"[\s_.\-—–/\\·:：()（）\[\]【】]+")
_PRIVATE_USE_CHARACTER = re.compile(r"[\ue000-\uf8ff]")
_APPROXIMATE_LABEL_MINIMUM_LENGTH = 6
_APPROXIMATE_LABEL_MINIMUM_SCORE = 0.8
_APPROXIMATE_LABEL_MINIMUM_MARGIN = 0.15
_TABLE_LABEL_QUALIFIER = re.compile(r"[（(]([^）)]+)[）)]")
_TABLE_LEVEL = re.compile(r"(?<![a-z0-9])[ivx\d一二三四五六七八九十]+级")
_RELATION_SCOPE_MODIFIER = re.compile(
    r"之前|之后|以前|以后|期间|过程中|"
    r"必须|应当|应该|不得|禁止|严禁|无需|不必|"
    r"至少|至多|仅限|只限|不超过|不低于"
)
_MIN_NAMED_LABEL_CHARS = 6
_MIN_LABEL_BASE_CHARS = 4
_MIN_LABEL_QUALIFIER_CHARS = 2
_MIN_TABLE_AXIS_CHARS = 2
_STRUCTURAL_NUMBER_PREFIX = re.compile(
    r"^\s*(?:(?:第[零一二三四五六七八九十百两\d]+(?:章|节|条|项)\s*)|"
    r"(?:(?:[1-9]\d{0,3}(?:[.．][1-9]\d{0,2}){0,5})|"
    r"[一二三四五六七八九十百]{1,4})"
    r"(?:[、.．)）]\s*|\s+|(?=[A-Za-z\u3400-\u9fff])))"
)
_DUTY_LABEL_SUFFIX = re.compile(
    r"(?:的)?(?:(?:岗位|安全|主要|核心|工作)?)职责$"
)
_ASCII_CASEFOLD = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def normalize_semantic_text(value: str) -> str:
    """统一 Unicode 宽度并仅折叠 ASCII 大小写。

    Args:
        value: 查询目标、结构标签或来源原文。

    Returns:
        保留非 ASCII 文字形态的稳定比较文本。

    """
    return unicodedata.normalize("NFKC", value).translate(_ASCII_CASEFOLD)


def named_table_label_in_query(query: str, label: str) -> bool:
    """同时核对表格行名及其限定词，容纳问句把两者换序。"""
    normalized_query = "".join(normalize_semantic_text(query).split())
    qualifier = _TABLE_LABEL_QUALIFIER.search(label)
    if qualifier is None:
        normalized_label = "".join(normalize_semantic_text(label).split())
        return (
            len(normalized_label) >= _MIN_NAMED_LABEL_CHARS
            and normalized_label in normalized_query
        )
    base = "".join(
        normalize_semantic_text(_TABLE_LABEL_QUALIFIER.sub("", label)).split()
    )
    scoped = "".join(normalize_semantic_text(qualifier[1]).split())
    if _TABLE_LEVEL.fullmatch(scoped):
        qualifier_matches = scoped in {
            match.group() for match in _TABLE_LEVEL.finditer(normalized_query)
        }
    else:
        qualifier_matches = scoped in normalized_query
    return (
        len(base) >= _MIN_LABEL_BASE_CHARS
        and len(scoped) >= _MIN_LABEL_QUALIFIER_CHARS
        and (
            base in normalized_query
            or (
                len(base) >= _MIN_NAMED_LABEL_CHARS
                and base[-_MIN_LABEL_BASE_CHARS:] in normalized_query
            )
        )
        and qualifier_matches
    )


def table_axis_label_in_query(query: str, label: str) -> bool:
    """检查问句是否逐字指定短表格轴标签。

    这个函数只在行名和列名同时命中时用于确定性关系证明。
    括号内的解释不作为列名命中的必要条件，但不允许单字
    标签通过，避免宽泛的字面偶合。

    Args:
        query: 当前 Atom 的原始问句片段。
        label: 可信物理表格的行名或列名。

    Returns:
        问句显式包含规范标签时返回 True。

    """
    normalized_query = normalize_document_label(query)
    base = _TABLE_LABEL_QUALIFIER.sub("", label)
    normalized_label = normalize_document_label(base)
    return (
        len(normalized_label) >= _MIN_TABLE_AXIS_CHARS
        and normalized_label in normalized_query
    )


def query_without_source_qualifier(
    query: str, source_qualifier: str | None
) -> str:
    """从关系核验文本中移除已解析的来源标签。

    来源身份已由独立合同保存，不能再让文档标题中的短词被当成
    用户询问的表格行名或列名。这里只移除第一次逐字命中，不解析
    新的来源，也不删除正文中其它相同词语。

    Args:
        query: 当前 Atom 的原始问句片段。
        source_qualifier: 已由来源解析器确认的文档标签。

    Returns:
        不含首个来源标签的关系核验文本。

    """
    if not source_qualifier:
        return query
    return query.replace(source_qualifier, "", 1)


def literal_relation_modifiers_supported(query: str, source_text: str) -> bool:
    """限定词只在来源逐字包含时参与确定性关系证明。

    这里不判定自然语言语义；不能逐字证明时仅返回未确定，
    由后续语义复核处理，不因此拒绝该来源。

    Args:
        query: 当前 Atom 的原始问句片段。
        source_text: 表头等可信结构来源文本。

    Returns:
        问句没有高风险限定词，或这些限定词全部在来源中时
        返回 True。

    """
    modifiers = tuple(
        dict.fromkeys(
            match.group(0) for match in _RELATION_SCOPE_MODIFIER.finditer(query)
        )
    )
    normalized_source = normalize_document_label(source_text)
    return all(
        normalize_document_label(modifier) in normalized_source
        for modifier in modifiers
    )


def normalize_identifier(identifier: str) -> str:
    """生成 NFKC、casefold 与分隔符统一后的 identifier。

    Args:
        identifier: 原始 identifier。

    Returns:
        用单个连字符连接的规范形式。

    """
    normalized = unicodedata.normalize("NFKC", identifier).casefold().strip()
    return re.sub(r"[\s_./\\-]+", "-", normalized).strip("-")


def normalize_document_label(value: str) -> str:
    """规范化用户输入或文档标签中的非语义格式差异。

    Args:
        value: 用户对象、文档显示名或根标题。

    Returns:
        去扩展名、统一大小写并移除结构分隔符的标签。

    """
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = _DOCUMENT_EXTENSION.sub("", normalized)
    return _STRUCTURAL_SEPARATOR.sub("", normalized)


def normalize_catalog_label(value: str) -> str:
    """目录标题同一性忽略实体编码和系统添加的原件格式说明。

    Args:
        value: 问题标题、目录项标题或显示名。

    Returns:
        不模糊匹配业务名称的规范标签。

    """
    normalized = unescape(unicodedata.normalize("NFKC", value).strip())
    normalized = _DOCUMENT_EXTENSION.sub("", normalized)
    return normalize_document_label(
        _CATALOG_ORIGINAL_SUFFIX.sub("", normalized)
    )


def normalize_duty_heading_label(value: str) -> str:
    """规范化职责标题，同时保留岗位名称的完整边界。

    Args:
        value: 查询主体或标题路径中的一个完整标题。

    Returns:
        去除章节编号和职责后缀后的精确标签；不会做子串或模糊匹配。

    """
    return _DUTY_LABEL_SUFFIX.sub("", normalize_section_heading_label(value))


def normalize_section_heading_label(value: str) -> str:
    """规范化通用章节标题，同时保留标题的完整语义边界。

    Args:
        value: 查询目标或标题路径中的一个完整标题。

    Returns:
        去除章节编号、格式分隔符和旧 Word 私用区标记后的精确标签。

    """
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = _STRUCTURAL_NUMBER_PREFIX.sub("", normalized)
    normalized = _STRUCTURAL_SEPARATOR.sub("", normalized)
    return _PRIVATE_USE_CHARACTER.sub("", normalized)


def section_heading_path_owns_target(
    target: str,
    heading_path: Iterable[str],
) -> bool:
    """检查最后一个有效标题是否与章节查询目标完全相同。

    Args:
        target: 已从章节问句中提取的完整目标。
        heading_path: Chunk 冻结的逐级标题路径。

    Returns:
        最深有效标题去格式后与查询目标完全相同时返回 True。

    """
    normalized_target = normalize_section_heading_label(target)
    labels = tuple(
        normalize_section_heading_label(heading) for heading in heading_path
    )
    return bool(normalized_target) and any(
        label == normalized_target and not any(labels[index + 1 :])
        for index, label in enumerate(labels)
    )


def duty_heading_path_owns_target(
    target: str,
    heading_path: Iterable[str],
) -> bool:
    """检查标题路径是否含有与查询主体完全相同的职责所有者。

    Args:
        target: 已从职责问句中提取的完整主体。
        heading_path: Chunk 冻结的逐级标题路径。

    Returns:
        某一级标题去格式后与主体完全相同时返回 True。

    """
    normalized_target = normalize_duty_heading_label(target)
    labels = tuple(
        normalize_duty_heading_label(heading) for heading in heading_path
    )
    return bool(normalized_target) and any(
        label == normalized_target and not any(labels[index + 1 :])
        for index, label in enumerate(labels)
    )


def context_label_variants(value: str) -> tuple[str, ...]:
    """保留“X 项目”场景并生成通用的“项目 X”标签次序。

    Args:
        value: 从高置信问句边界提取的场景对象。

    Returns:
        原对象、项目名前置形式和不带载体词的核心，稳定去重。

    """
    normalized = unicodedata.normalize("NFKC", value).strip()
    variants = [normalized]
    if normalized.endswith("项目"):
        core = normalized[: -len("项目")].strip()
        if core:
            variants.extend((f"项目{core}", core))
    return tuple(dict.fromkeys(item for item in variants if item))


def select_unique_label_owner(
    target: str,
    labels: Iterable[tuple[str, str]],
) -> str | None:
    """在严格包含或高相似且唯一时解析动态文档对象。

    近似匹配只处理未指定来源的自然对象说法。短标签、并列最优或没有
    足够领先幅度的候选全部返回 ``None``，由上层保持歧义或拒答。

    Args:
        target: 用户问题中的动态文档对象，不是业务实体白名单。
        labels: ``(owner_id, display_or_root_label)`` 序列。

    Returns:
        唯一标签归属；无法安全确定时返回 ``None``。

    """
    normalized_target = normalize_document_label(target)
    if not normalized_target:
        return None
    normalized_labels = tuple(
        (owner_id, normalize_document_label(label))
        for owner_id, label in labels
        if owner_id and normalize_document_label(label)
    )
    exact = {
        owner_id
        for owner_id, label in normalized_labels
        if normalized_target in label
    }
    if len(exact) == 1:
        return next(iter(exact))
    if exact or len(normalized_target) < _APPROXIMATE_LABEL_MINIMUM_LENGTH:
        return None
    scores: dict[str, float] = {}
    for owner_id, label in normalized_labels:
        score = SequenceMatcher(
            None,
            normalized_target,
            label,
            autojunk=False,
        ).ratio()
        scores[owner_id] = max(scores.get(owner_id, 0.0), score)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] < _APPROXIMATE_LABEL_MINIMUM_SCORE:
        return None
    if (
        len(ranked) > 1
        and ranked[0][1] - ranked[1][1] < _APPROXIMATE_LABEL_MINIMUM_MARGIN
    ):
        return None
    return ranked[0][0]


__all__ = [
    "context_label_variants",
    "duty_heading_path_owns_target",
    "literal_relation_modifiers_supported",
    "named_table_label_in_query",
    "normalize_document_label",
    "normalize_duty_heading_label",
    "normalize_identifier",
    "normalize_section_heading_label",
    "normalize_semantic_text",
    "query_without_source_qualifier",
    "section_heading_path_owns_target",
    "select_unique_label_owner",
    "table_axis_label_in_query",
]
