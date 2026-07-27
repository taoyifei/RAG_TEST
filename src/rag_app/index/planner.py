"""根据目录快照生成确定性的增量索引计划。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum

from rag_app.state.models import ActiveSource

__all__ = [
    "DiscoveredSource",
    "SyncAction",
    "SyncActionKind",
    "SyncPlan",
    "plan_incremental_sync",
]

_SHA256_HEX_LENGTH = 64


class SyncActionKind(StrEnum):
    """目录快照相对活动 manifest 的变化类型。"""

    RENAME = "rename"
    UPDATE = "update"
    ADD = "add"
    DELETE = "delete"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class DiscoveredSource:
    """本次扫描发现的一个 DOCX。"""

    source_path: str
    content_sha256: str

    def __post_init__(self) -> None:
        """拒绝空路径和无效内容摘要。"""
        if not self.source_path:
            raise ValueError("source_path 不能为空。")
        _require_sha256(self.content_sha256)


@dataclass(frozen=True, slots=True)
class SyncAction:
    """一个可持久化、可幂等执行的同步动作。"""

    kind: SyncActionKind
    source_id: str | None
    previous_path: str | None
    source_path: str | None
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """排序和摘要均稳定的目录同步计划。"""

    actions: tuple[SyncAction, ...]
    digest: str


def plan_incremental_sync(
    discovered: tuple[DiscoveredSource, ...],
    active: tuple[ActiveSource, ...],
) -> SyncPlan:
    """比较目录快照与活动来源。

    Args:
        discovered: 本次扫描的全部 DOCX。
        active: SQLite 中当前全部活动来源。

    Returns:
        覆盖新增、修改、唯一重命名、删除和不变项的确定性计划。

    Raises:
        ValueError: 输入含重复路径。

    """
    discovered_by_path = _unique_discovered_paths(discovered)
    active_by_path = _unique_active_paths(active)
    used_discovered: set[str] = set()
    used_active: set[str] = set()
    grouped: dict[SyncActionKind, list[SyncAction]] = defaultdict(list)

    for path in sorted(discovered_by_path.keys() & active_by_path.keys()):
        found = discovered_by_path[path]
        current = active_by_path[path]
        kind = (
            SyncActionKind.UNCHANGED
            if found.content_sha256 == current.content_sha256
            else SyncActionKind.UPDATE
        )
        grouped[kind].append(
            SyncAction(
                kind=kind,
                source_id=current.source_id,
                previous_path=path,
                source_path=path,
                content_sha256=found.content_sha256,
            )
        )
        used_discovered.add(path)
        used_active.add(path)

    unmatched_discovered = [
        item
        for item in discovered
        if item.source_path not in used_discovered
    ]
    unmatched_active = [
        item for item in active if item.current_path not in used_active
    ]
    discovered_by_hash = _group_discovered_by_hash(unmatched_discovered)
    active_by_hash = _group_active_by_hash(unmatched_active)
    for digest in sorted(discovered_by_hash.keys() & active_by_hash.keys()):
        found_items = discovered_by_hash[digest]
        current_items = active_by_hash[digest]
        if len(found_items) != 1 or len(current_items) != 1:
            continue
        found = found_items[0]
        current = current_items[0]
        grouped[SyncActionKind.RENAME].append(
            SyncAction(
                kind=SyncActionKind.RENAME,
                source_id=current.source_id,
                previous_path=current.current_path,
                source_path=found.source_path,
                content_sha256=digest,
            )
        )
        used_discovered.add(found.source_path)
        used_active.add(current.current_path)

    for found in discovered:
        if found.source_path in used_discovered:
            continue
        grouped[SyncActionKind.ADD].append(
            SyncAction(
                kind=SyncActionKind.ADD,
                source_id=None,
                previous_path=None,
                source_path=found.source_path,
                content_sha256=found.content_sha256,
            )
        )
    for current in active:
        if current.current_path in used_active:
            continue
        grouped[SyncActionKind.DELETE].append(
            SyncAction(
                kind=SyncActionKind.DELETE,
                source_id=current.source_id,
                previous_path=current.current_path,
                source_path=None,
                content_sha256=current.content_sha256,
            )
        )

    actions = tuple(
        action
        for kind in SyncActionKind
        for action in sorted(
            grouped[kind],
            key=_action_sort_key,
        )
    )
    return SyncPlan(actions=actions, digest=_plan_digest(actions))


def _unique_discovered_paths(
    sources: tuple[DiscoveredSource, ...],
) -> dict[str, DiscoveredSource]:
    result = {source.source_path: source for source in sources}
    if len(result) != len(sources):
        raise ValueError("目录快照含重复 source_path。")
    return result


def _unique_active_paths(
    sources: tuple[ActiveSource, ...],
) -> dict[str, ActiveSource]:
    result = {source.current_path: source for source in sources}
    if len(result) != len(sources):
        raise ValueError("活动来源含重复 current_path。")
    return result


def _group_discovered_by_hash(
    sources: list[DiscoveredSource],
) -> dict[str, list[DiscoveredSource]]:
    grouped: dict[str, list[DiscoveredSource]] = defaultdict(list)
    for source in sources:
        grouped[source.content_sha256].append(source)
    return grouped


def _group_active_by_hash(
    sources: list[ActiveSource],
) -> dict[str, list[ActiveSource]]:
    grouped: dict[str, list[ActiveSource]] = defaultdict(list)
    for source in sources:
        grouped[source.content_sha256].append(source)
    return grouped


def _action_sort_key(action: SyncAction) -> tuple[str, str]:
    return (
        action.source_path or action.previous_path or "",
        action.source_id or "",
    )


def _plan_digest(actions: tuple[SyncAction, ...]) -> str:
    serialized = json.dumps(
        [
            {
                **asdict(action),
                "kind": action.kind.value,
            }
            for action in actions
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"


def _require_sha256(value: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("content_sha256 必须是 64 位小写十六进制。")
