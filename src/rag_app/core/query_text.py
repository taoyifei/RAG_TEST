"""查询文本的基础规范化规则。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher

_DOCUMENT_EXTENSION = re.compile(r"\.(?:docx?|pdf|txt|md)$")
_STRUCTURAL_SEPARATOR = re.compile(r"[\s_.\-—–/\\·:：()（）\[\]【】]+")
_APPROXIMATE_LABEL_MINIMUM_LENGTH = 6
_APPROXIMATE_LABEL_MINIMUM_SCORE = 0.8
_APPROXIMATE_LABEL_MINIMUM_MARGIN = 0.15
_STRUCTURAL_NUMBER_PREFIX = re.compile(
    r"^\s*(?:(?:第[零一二三四五六七八九十百两\d]+(?:章|节|条|项)\s*)|"
    r"(?:(?:[1-9]\d{0,3}(?:[.．][1-9]\d{0,2}){0,5})|"
    r"[一二三四五六七八九十百]{1,4})"
    r"(?:[、.．)）]\s*|\s+|(?=[A-Za-z\u3400-\u9fff])))"
)
_DUTY_LABEL_SUFFIX = re.compile(
    r"(?:的)?(?:(?:岗位|安全|主要|核心|工作)?)职责$"
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


def normalize_duty_heading_label(value: str) -> str:
    """规范化职责标题，同时保留岗位名称的完整边界。

    Args:
        value: 查询主体或标题路径中的一个完整标题。

    Returns:
        去除章节编号和职责后缀后的精确标签；不会做子串或模糊匹配。

    """
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = _STRUCTURAL_NUMBER_PREFIX.sub("", normalized)
    normalized = _STRUCTURAL_SEPARATOR.sub("", normalized)
    return _DUTY_LABEL_SUFFIX.sub("", normalized)


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
    "normalize_document_label",
    "normalize_duty_heading_label",
    "normalize_identifier",
    "select_unique_label_owner",
]
