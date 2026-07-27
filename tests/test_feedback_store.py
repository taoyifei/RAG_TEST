"""用户反馈只保存非敏感验收信号。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.state.feedback import FeedbackStore


def test_feedback_is_idempotently_updated_by_trace_id(tmp_path: Path) -> None:
    """同一回答的重复反馈必须更新而不是重复累计。"""
    store = FeedbackStore(tmp_path / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)

    store.record("a" * 32, useful=True, now=now)
    store.record(
        "a" * 32,
        useful=False,
        now=now + timedelta(seconds=1),
    )

    assert store.counts() == {"useful": 0, "not_useful": 1, "total": 1}
