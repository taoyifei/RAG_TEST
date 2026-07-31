from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.generation.answer import (
    AnswerClaim,
    AnswerResult,
    AnswerStatus,
    ClaimSupport,
    RefusalCode,
)
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


def test_conversation_persists_only_verified_claim_summary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    store = ConversationStore(
        database_path,
        ttl_seconds=60,
        max_rounds=2,
    )
    store.initialize()
    now = datetime(2026, 7, 31, 1, 0, tzinfo=UTC)
    answer = AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer="方案甲。\n\n方案乙。",
        claims=(
            AnswerClaim(
                text="方案甲。",
                supports=(
                    ClaimSupport(
                        evidence_id="EVIDENCE_ID_MUST_NOT_PERSIST_1",
                        chunk_id="chunk_" + "1" * 32,
                        quote="QUOTE_MUST_NOT_PERSIST_1",
                        locator="规范.docx > 段落1",
                    ),
                ),
            ),
            AnswerClaim(
                text="方案乙。",
                supports=(
                    ClaimSupport(
                        evidence_id="EVIDENCE_ID_MUST_NOT_PERSIST_2",
                        chunk_id="chunk_" + "2" * 32,
                        quote="QUOTE_MUST_NOT_PERSIST_2",
                        locator="规范.docx > 段落2",
                    ),
                ),
            ),
        ),
        refusal_code=None,
        model_calls=1,
        calls=(),
        trace={"raw_output": "DO_NOT_PERSIST_RAW_MODEL_OUTPUT"},
    )

    store.append_turn(
        "conversation-1",
        "有哪些方案？",
        answer=answer,
        now=now,
        turn_id="turn-1",
    )
    context = store.get_rewrite_context("conversation-1", now=now)

    assert context.questions == ("有哪些方案？",)
    assert [claim.text for claim in context.verified_claims] == [
        "方案甲。",
        "方案乙。",
    ]
    assert context.verified_claims[1].supports[0].chunk_id == (
        "chunk_" + "2" * 32
    )
    assert context.verified_claims[1].supports[0].locator == (
        "规范.docx > 段落2"
    )
    persisted_bytes = b"".join(
        path.read_bytes()
        for path in tmp_path.glob("state.sqlite3*")
        if path.is_file()
    )
    assert b"DO_NOT_PERSIST_RAW_MODEL_OUTPUT" not in persisted_bytes
    assert b"EVIDENCE_ID_MUST_NOT_PERSIST" not in persisted_bytes
    assert b"QUOTE_MUST_NOT_PERSIST" not in persisted_bytes

    store.append_turn(
        "conversation-1",
        "没有答案的问题",
        answer=AnswerResult(
            status=AnswerStatus.REFUSED,
            answer=None,
            claims=(),
            refusal_code=RefusalCode.NO_EVIDENCE,
            model_calls=0,
            calls=(),
        ),
        now=now + timedelta(seconds=1),
        turn_id="turn-2",
    )

    refused_context = store.get_rewrite_context(
        "conversation-1",
        now=now + timedelta(seconds=1),
    )
    assert refused_context.questions == (
        "有哪些方案？",
        "没有答案的问题",
    )
    assert refused_context.verified_claims == ()
