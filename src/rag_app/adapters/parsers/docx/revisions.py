"""DOCX tracked changes 的固定视图策略。"""

from __future__ import annotations

from lxml import etree

from rag_app.adapters.parsers.docx.namespaces import local_name, word_attr
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import RevisionMark
from rag_app.core.policies import ParsingPolicy, TrackedChangesPolicy

_INCLUDED_FINAL = frozenset({"ins", "moveTo"})
_EXCLUDED_FINAL = frozenset({"del", "moveFrom"})
_REVISION_NAMES = _INCLUDED_FINAL | _EXCLUDED_FINAL


def revision_visibility(
    node: etree._Element,
    policy: ParsingPolicy,
) -> tuple[bool, RevisionMark | None]:
    """判断修订 wrapper 在当前视图中是否可见。

    Args:
        node: 可能是修订 wrapper 的 OOXML 元素。
        policy: tracked changes 策略。

    Returns:
        `(是否遍历子树, 修订标记)`。

    Raises:
        InvalidDocument: `reject` 策略遇到任意修订。

    """
    kind = local_name(node)
    if kind not in _REVISION_NAMES:
        return True, None
    if policy.tracked_changes is TrackedChangesPolicy.REJECT:
        raise InvalidDocument(
            "ParsingPolicy 拒绝包含修订的 DOCX。",
            stage="docx-ooxml-v4.revisions",
        )
    mark = RevisionMark(
        kind=kind,
        author=node.get(word_attr("author")),
        timestamp=node.get(word_attr("date")),
    )
    if policy.tracked_changes is TrackedChangesPolicy.FINAL_VIEW:
        return kind in _INCLUDED_FINAL, mark
    return kind in _INCLUDED_FINAL, mark


def has_revision(node: etree._Element) -> bool:
    """判断子树是否包含 tracked changes wrapper。

    Args:
        node: 待检查的 OOXML 元素。

    Returns:
        找到修订 wrapper 时为 `True`。

    """
    return any(local_name(item) in _REVISION_NAMES for item in node.iter())
