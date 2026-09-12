"""从缺失样式的正文段落恢复保守、可审计的编号标题。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from rag_app.core.models import DocumentNode, NodeKind

_ARABIC_NUMBERED_HEADING = re.compile(
    r"^\s*(?P<number>[1-9]\d{0,3}(?:[.．][1-9]\d{0,2}){0,5})"
    r"(?P<separator>[、.．)）]\s*|\s+|(?=[A-Za-z\u3400-\u9fff]))"
    r"(?P<title>\S.*?)\s*$"
)
_CHINESE_NUMBERED_HEADING = re.compile(
    r"^\s*(?P<number>[一二三四五六七八九十百]{1,4})"
    r"(?P<separator>[、.．])\s*(?P<title>\S.*?)\s*$"
)
_TERMINAL_SENTENCE_PUNCTUATION = re.compile(r"[。；;][\s\ue000-\uf8ff]*$")
_TITLE_TEXT = re.compile(r"[A-Za-z\u3400-\u9fff]")
_MEASUREMENT_TITLE = re.compile(
    r"^(?:%|％|分(?:数)?|万?元|人民币|美元|欧元|kg|g|mg|mm|cm|m|km|"
    r"mpa|kpa|pa|℃|°c)(?:\s|$|[（(])",
    re.IGNORECASE,
)
_STRONG_CLAUSE_MARKER = re.compile(
    r"必须|不得|应当|应该|均应|都应|应于|应依|应按|应附|方可|予以|"
    r"(?:前|后|时)(?:再)?(?:办理|进行|加刻|联系|执行|填写|交|送|报)"
)
_ACTOR_ACTION_PREFIX = re.compile(
    r"^(?:本?公司|公司全体员工|各(?:部门|岗位|单位|人员)|"
    r"[A-Za-z\u3400-\u9fff]{1,16}(?:部|部门|人员|员工|主管|经理|"
    r"操作者|使用人|单位|小组|中心|科|处))"
    r"(?:都|均)?(?:应|需|必须|不得|负责(?!人)|根据|按照|依照|"
    r"进行|执行|填写|通知|制定|组织|联系|核对)"
)
_MAX_TITLE_CHARACTERS = 32
_MAX_TOP_LEVEL_NUMBER = 99
_MIN_TOC_CLUSTER_SIZE = 3


@dataclass(frozen=True, slots=True)
class InferredHeading:
    """一个仍指向真实节点的编号标题结构。"""

    node_id: str
    label: str
    level: int
    family: str
    sequence: tuple[int, ...]
    marker: str

    @property
    def ordinal(self) -> int:
        """返回当前编号层级的末位序号。"""
        return self.sequence[-1]


def infer_numbered_headings(
    nodes: Sequence[DocumentNode],
) -> dict[str, InferredHeading]:
    """识别普通段落中的编号标题并抑制连续目录项。

    Args:
        nodes: 已排除表格和文本框后、按来源顺序排列的 BODY 节点。

    Returns:
        以真实段落 node ID 为键的保守推断结果。

    """
    candidates = {
        node.node_id: candidate
        for node in nodes
        if (candidate := _candidate(node)) is not None
    }
    suppressed = _toc_node_ids(nodes, candidates)
    return {
        node_id: candidate
        for node_id, candidate in candidates.items()
        if node_id not in suppressed
    }


def classify_numbered_heading(
    node_id: str,
    label: str,
) -> InferredHeading | None:
    """解析已知标题文本的编号结构，不决定节点是否应提升为标题。

    Args:
        node_id: 标题对应的真实 Document IR 节点 ID。
        label: 已去除首尾空白的标题文本。

    Returns:
        文本符合受支持编号形态时返回结构信息，否则返回 None。

    """
    return _arabic_candidate(node_id, label) or _chinese_candidate(
        node_id, label
    )


def _candidate(node: DocumentNode) -> InferredHeading | None:
    if node.kind is not NodeKind.PARAGRAPH or node.text_payload is None:
        return None
    label = node.text_payload.exact_text.strip()
    if not label:
        return None
    return classify_numbered_heading(node.node_id, label)


def _arabic_candidate(node_id: str, label: str) -> InferredHeading | None:
    arabic = _ARABIC_NUMBERED_HEADING.fullmatch(label)
    if arabic is None:
        return None
    sequence = tuple(int(part) for part in re.split(r"[.．]", arabic["number"]))
    if sequence[0] > _MAX_TOP_LEVEL_NUMBER or not _looks_like_title(
        arabic["title"].strip()
    ):
        return None
    return InferredHeading(
        node_id=node_id,
        label=label,
        level=len(sequence),
        family="arabic",
        sequence=sequence,
        marker=_normalized_marker(arabic["separator"]),
    )


def _chinese_candidate(node_id: str, label: str) -> InferredHeading | None:
    chinese = _CHINESE_NUMBERED_HEADING.fullmatch(label)
    if chinese is None or not _looks_like_title(chinese["title"].strip()):
        return None
    ordinal = _chinese_number(chinese["number"])
    if ordinal is None:
        return None
    # 单层中文序号既可能是主节，也可能位于阿拉伯主节之下；最终层级由
    # 当前活跃编号序列决定，不在词法识别阶段伪造固定父级。
    return InferredHeading(
        node_id=node_id,
        label=label,
        level=1,
        family="chinese",
        sequence=(ordinal,),
        marker=_normalized_marker(chinese["separator"]),
    )


def resolve_heading_level(
    candidate: InferredHeading,
    active: Sequence[InferredHeading | None],
) -> int:
    """结合当前编号序列确定平铺标题的相对层级。

    Args:
        candidate: 当前待放入标题栈的保守推断标题。
        active: 当前标题路径逐级对应的编号结构；无编号标题用 None。

    Returns:
        从 1 开始的目标层级。缺失中间级时不会制造空层级。

    """
    if len(candidate.sequence) > 1:
        return _hierarchical_level(candidate, active)
    return _flat_level(candidate, active)


def _hierarchical_level(
    candidate: InferredHeading,
    active: Sequence[InferredHeading | None],
) -> int:
    prefix = candidate.sequence[:-1]
    siblings = [
        index
        for index, state in enumerate(active)
        if state is not None
        and state.family == candidate.family
        and len(state.sequence) == len(candidate.sequence)
        and state.sequence[:-1] == prefix
    ]
    if siblings:
        return siblings[-1] + 1
    parents = [
        index
        for index, state in enumerate(active)
        if state is not None
        and state.family == candidate.family
        and state.sequence == prefix
    ]
    if parents:
        return min(parents[-1] + 2, len(active) + 1)
    matching_roots = [
        index
        for index, state in enumerate(active)
        if state is not None
        and state.family == candidate.family
        and state.sequence == candidate.sequence[:1]
    ]
    if matching_roots:
        return min(
            matching_roots[0] + len(candidate.sequence),
            len(active) + 1,
        )
    # 编号前缀找不到真实父标题时，只保留当前真实节点；不能把 4.1
    # 错挂到仍活跃的“3 职责”，也不能伪造并不存在的“4”。
    return 1


def _flat_level(
    candidate: InferredHeading,
    active: Sequence[InferredHeading | None],
) -> int:
    if not active:
        return 1

    continuations = [
        (index, state)
        for index, state in enumerate(active)
        if state is not None
        and len(state.sequence) == 1
        and state.family == candidate.family
        and candidate.ordinal == state.ordinal + 1
    ]
    if continuations:
        marker_matches = [
            item for item in continuations if item[1].marker == candidate.marker
        ]
        selected = max(
            marker_matches or continuations,
            key=lambda item: item[0],
        )
        return selected[0] + 1

    root = active[0]
    if (
        root is not None
        and root.family == candidate.family
        and candidate.ordinal > root.sequence[0]
    ):
        return 1

    for index in range(len(active) - 1, 0, -1):
        state = active[index]
        if (
            state is not None
            and len(state.sequence) == 1
            and state.family == candidate.family
            and state.marker == candidate.marker
            and candidate.ordinal >= state.ordinal
        ):
            return index + 1
    return min(len(active) + 1, 6)


def _looks_like_title(title: str) -> bool:
    compact = "".join(title.split())
    return bool(
        compact
        and len(compact) <= _MAX_TITLE_CHARACTERS
        and _TITLE_TEXT.search(compact)
        and _TERMINAL_SENTENCE_PUNCTUATION.search(title) is None
        and _MEASUREMENT_TITLE.match(compact) is None
        and _STRONG_CLAUSE_MARKER.search(title) is None
        and _ACTOR_ACTION_PREFIX.search(compact) is None
    )


def _normalized_marker(value: str) -> str:
    if not value:
        return "adjacent"
    if value.isspace():
        return "space"
    if value in {".", "．"}:
        return "dot"
    if value == "、":
        return "comma"
    return "parenthesis"


def _toc_node_ids(
    nodes: Sequence[DocumentNode],
    candidates: dict[str, InferredHeading],
) -> set[str]:
    suppressed: set[str] = set()
    pending: list[InferredHeading] = []

    def flush() -> None:
        if _looks_like_toc_cluster(pending):
            suppressed.update(item.node_id for item in pending)
        pending.clear()

    for node in nodes:
        candidate = candidates.get(node.node_id)
        if candidate is not None:
            key = (candidate.family, candidate.level)
            numbering_reset = bool(
                pending
                and key == (pending[0].family, pending[0].level)
                and candidate.ordinal < pending[-1].ordinal
            )
            if pending and (
                key != (pending[0].family, pending[0].level) or numbering_reset
            ):
                flush()
            pending.append(candidate)
            continue
        if node.text.strip() or node.kind not in {
            NodeKind.PARAGRAPH,
            NodeKind.LIST_ITEM,
        }:
            flush()
    flush()
    return suppressed


def _looks_like_toc_cluster(items: Sequence[InferredHeading]) -> bool:
    if len(items) < _MIN_TOC_CLUSTER_SIZE:
        return False
    ordinals = [item.ordinal for item in items]
    return len(set(ordinals)) >= _MIN_TOC_CLUSTER_SIZE and all(
        right >= left for left, right in pairwise(ordinals)
    )


def _chinese_number(value: str) -> int | None:
    digits = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value in digits:
        return digits[value]
    if value == "十":
        return 10
    if value.startswith("十"):
        return 10 + digits.get(value[1:], 0)
    if "十" in value:
        head, _, tail = value.partition("十")
        return digits.get(head, 0) * 10 + digits.get(tail, 0)
    return None


__all__ = [
    "InferredHeading",
    "classify_numbered_heading",
    "infer_numbered_headings",
    "resolve_heading_level",
]
