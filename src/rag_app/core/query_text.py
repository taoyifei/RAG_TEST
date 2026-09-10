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
    "normalize_document_label",
    "normalize_identifier",
    "select_unique_label_owner",
]
