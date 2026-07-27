from rag_app.index.planner import (
    DiscoveredSource,
    SyncActionKind,
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
