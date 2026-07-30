from rag_app.index.planner import (
    DiscoveredSource,
    SyncActionKind,
    plan_full_rebuild,
    plan_incremental_sync,
)
from rag_app.state import ActiveSource


def _active(
    source_id: str,
    path: str,
    digest_character: str,
) -> ActiveSource:
    digest = digest_character * 64
    return ActiveSource(
        source_id=source_id,
        current_path=path,
        content_sha256=digest,
        doc_version=f"sha256:{digest}",
    )


def test_incremental_plan_covers_all_changes_without_ambiguous_rename() -> None:
    active = (
        _active("source-rename", "旧.docx", "a"),
        _active("source-update", "修改.docx", "b"),
        _active("source-delete", "删除.docx", "c"),
        _active("source-dup-1", "重复一.docx", "d"),
        _active("source-dup-2", "重复二.docx", "d"),
        _active("source-same", "不变.docx", "e"),
    )
    discovered = (
        DiscoveredSource("新.docx", "a" * 64),
        DiscoveredSource("修改.docx", "f" * 64),
        DiscoveredSource("新增.docx", "0" * 64),
        DiscoveredSource("歧义副本.docx", "d" * 64),
        DiscoveredSource("不变.docx", "e" * 64),
    )

    plan = plan_incremental_sync(discovered, active)

    assert [
        (
            action.kind,
            action.source_id,
            action.previous_path,
            action.source_path,
        )
        for action in plan.actions
    ] == [
        (
            SyncActionKind.RENAME,
            "source-rename",
            "旧.docx",
            "新.docx",
        ),
        (
            SyncActionKind.UPDATE,
            "source-update",
            "修改.docx",
            "修改.docx",
        ),
        (
            SyncActionKind.ADD,
            None,
            None,
            "新增.docx",
        ),
        (
            SyncActionKind.ADD,
            None,
            None,
            "歧义副本.docx",
        ),
        (
            SyncActionKind.DELETE,
            "source-delete",
            "删除.docx",
            None,
        ),
        (
            SyncActionKind.DELETE,
            "source-dup-1",
            "重复一.docx",
            None,
        ),
        (
            SyncActionKind.DELETE,
            "source-dup-2",
            "重复二.docx",
            None,
        ),
        (
            SyncActionKind.UNCHANGED,
            "source-same",
            "不变.docx",
            "不变.docx",
        ),
    ]
    assert plan.digest.startswith("sha256:")


def test_plan_is_order_independent() -> None:
    active = (
        _active("source-a", "甲.docx", "a"),
        _active("source-b", "乙.docx", "b"),
    )
    discovered = (
        DiscoveredSource("甲.docx", "a" * 64),
        DiscoveredSource("丙.docx", "c" * 64),
    )

    forward = plan_incremental_sync(discovered, active)
    reversed_plan = plan_incremental_sync(
        tuple(reversed(discovered)),
        tuple(reversed(active)),
    )

    assert reversed_plan == forward


def test_full_plan_rebuilds_all_sources_and_preserves_safe_identity() -> None:
    active = (
        _active("source-same-path", "同路径.docx", "a"),
        _active("source-rename", "旧名称.docx", "b"),
        _active("source-unchanged", "不变.docx", "c"),
        _active("source-deleted", "已删除.docx", "d"),
    )
    discovered = (
        DiscoveredSource("同路径.docx", "e" * 64),
        DiscoveredSource("新名称.docx", "b" * 64),
        DiscoveredSource("不变.docx", "c" * 64),
        DiscoveredSource("新增.docx", "f" * 64),
    )

    plan = plan_full_rebuild(discovered, active)

    assert [
        (
            action.kind,
            action.source_path,
            action.source_id_hint,
        )
        for action in plan.actions
    ] == [
        (SyncActionKind.ADD, "不变.docx", "source-unchanged"),
        (SyncActionKind.ADD, "同路径.docx", "source-same-path"),
        (SyncActionKind.ADD, "新名称.docx", "source-rename"),
        (SyncActionKind.ADD, "新增.docx", None),
    ]
    assert all(
        action.previous_path is None and action.source_id is None
        for action in plan.actions
    )


def test_full_plan_does_not_guess_duplicate_content_or_rename_update() -> None:
    active = (
        _active("source-duplicate-a", "重复甲.docx", "a"),
        _active("source-duplicate-b", "重复乙.docx", "a"),
        _active("source-renamed-updated", "旧路径.docx", "b"),
    )
    discovered = (
        DiscoveredSource("副本甲.docx", "a" * 64),
        DiscoveredSource("副本乙.docx", "a" * 64),
        DiscoveredSource("新路径.docx", "c" * 64),
    )

    plan = plan_full_rebuild(discovered, active)

    assert all(action.source_id_hint is None for action in plan.actions)


def test_full_plan_is_deterministic_for_reordered_inputs() -> None:
    active = (
        _active("source-a", "甲.docx", "a"),
        _active("source-b", "旧乙.docx", "b"),
    )
    discovered = (
        DiscoveredSource("甲.docx", "c" * 64),
        DiscoveredSource("新乙.docx", "b" * 64),
    )

    forward = plan_full_rebuild(discovered, active)
    reversed_plan = plan_full_rebuild(
        tuple(reversed(discovered)),
        tuple(reversed(active)),
    )

    assert reversed_plan == forward
