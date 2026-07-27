from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.state.conversations import ConversationStore


def test_conversation_ttl_round_limit_and_clear(tmp_path: Path) -> None:
    store = ConversationStore(
        tmp_path / "state.sqlite3",
        ttl_seconds=60,
        max_rounds=2,
    )
    store.initialize()
    now = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)

    store.append_question("conversation-1", "问题一", now=now)
    store.append_question(
        "conversation-1",
        "问题二",
        now=now + timedelta(seconds=1),
    )
    store.append_question(
        "conversation-1",
        "问题三",
        now=now + timedelta(seconds=2),
    )

    assert store.get_questions(
        "conversation-1",
        now=now + timedelta(seconds=3),
    ) == ("问题二", "问题三")
    assert store.get_questions(
        "conversation-1",
        now=now + timedelta(seconds=63),
    ) == ()

    store.append_question(
        "conversation-1",
        "新问题",
        now=now + timedelta(seconds=64),
    )
    store.clear("conversation-1")
    assert store.get_questions(
        "conversation-1",
        now=now + timedelta(seconds=65),
    ) == ()
