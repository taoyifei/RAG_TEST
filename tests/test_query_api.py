import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from rag_app.api.app import ApiServices, create_app
from rag_app.generation.answer import (
    AnswerClaim,
    AnswerResult,
    AnswerStatus,
    ClaimSupport,
)
from rag_app.health import ComponentStatus, ReadinessService
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import (
    AnswerStartEvent,
    QueryOutcome,
    StageEvent,
    StageName,
    ValidatedClaimEvent,
)
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore


@dataclass(frozen=True, slots=True)
class _ReadyProbe:
    def check(self) -> ComponentStatus:
        return ComponentStatus("local", True, "ready", 1, 1)


class _Query:
    def ask(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        del conversation_id, now
        assert question == "核验问题"
        emit(
            StageEvent(
                trace_id=trace_id,
                stage=StageName.RETRIEVE,
                elapsed_ms=12,
                metrics={"candidate_count": 3},
            )
        )
        answer = AnswerResult(
            status=AnswerStatus.ANSWERED,
            answer="已验证答案",
            claims=(
                AnswerClaim(
                    text="已验证答案",
                    supports=(
                        ClaimSupport(
                            evidence_id="E1",
                            chunk_id="chunk-1",
                            quote="证据原文",
                            locator="规范.docx > 第一章 > 段落 2",
                        ),
                    ),
                ),
            ),
            refusal_code=None,
            model_calls=1,
            calls=(),
        )
        return QueryOutcome(
            trace_id=trace_id,
            answer=answer,
            rewritten=False,
            stage_count=1,
        )

    def ask_stream(  # noqa: PLR0913
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
        emit_answer: Callable[
            [AnswerStartEvent | ValidatedClaimEvent],
            None,
        ],
        cancellation: object,
    ) -> QueryOutcome:
        del emit_answer, cancellation
        return self.ask(
            trace_id=trace_id,
            conversation_id=conversation_id,
            question=question,
            now=now,
            emit=emit,
        )


def _client(
    tmp_path: Path,
    *,
    query: object | None = None,
) -> tuple[TestClient, str, str]:
    query_token = uuid.uuid4().hex
    admin_token = uuid.uuid4().hex
    conversations = ConversationStore(
        tmp_path / "state.sqlite3",
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    jobs = JobStore(tmp_path / "state.sqlite3")
    jobs.initialize()
    feedback = FeedbackStore(tmp_path / "state.sqlite3")
    feedback.initialize()
    readiness = ReadinessService((_ReadyProbe(),))
    readiness.refresh_once()
    app = create_app(
        ApiServices(
            readiness=readiness,
            query_token=query_token,
            admin_token=admin_token,
            query=query or _Query(),  # type: ignore[arg-type]
            query_executor=QueryExecutor(),
            conversations=conversations,
            jobs=jobs,
            feedback=feedback,
            pipeline_fingerprint="pipeline-1",
            frontend_dir=Path(__file__).parents[1] / "frontend",
        )
    )
    return TestClient(app), query_token, admin_token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_chat_streams_only_stages_before_validated_final(
    tmp_path: Path,
) -> None:
    client, query_token, _ = _client(tmp_path)

    response = client.post(
        "/api/chat",
        headers=_bearer(query_token),
        json={
            "conversation_id": "conversation-1",
            "question": "核验问题",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0] == {
        "type": "stage",
        "trace_id": events[0]["trace_id"],
        "stage": "retrieve",
        "elapsed_ms": 12,
        "metrics": {"candidate_count": 3},
    }
    assert "answer" not in events[0]
    assert events[1]["type"] == "final"
    assert events[1]["answer"] == "已验证答案"
    assert events[1]["claims"][0]["supports"][0]["chunk_id"] == "chunk-1"
    assert events[1]["answer_mode"] == "ANSWERED"
    assert events[1]["user_message"] is None


class _StreamingQuery(_Query):
    def ask_stream(  # noqa: PLR0913
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
        emit_answer: Callable[
            [AnswerStartEvent | ValidatedClaimEvent],
            None,
        ],
        cancellation: object,
    ) -> QueryOutcome:
        del cancellation
        emit_answer(AnswerStartEvent(trace_id=trace_id, elapsed_ms=20))
        outcome = super().ask(
            trace_id=trace_id,
            conversation_id=conversation_id,
            question=question,
            now=now,
            emit=emit,
        )
        emit_answer(
            ValidatedClaimEvent(
                trace_id=trace_id,
                elapsed_ms=30,
                claim_index=0,
                claim=outcome.answer.claims[0],
            )
        )
        return outcome


def test_chat_streams_validated_claim_before_canonical_final(
    tmp_path: Path,
) -> None:
    client, query_token, _ = _client(tmp_path, query=_StreamingQuery())

    response = client.post(
        "/api/chat",
        headers=_bearer(query_token),
        json={
            "conversation_id": "conversation-1",
            "question": "核验问题",
        },
    )

    events = [json.loads(line) for line in response.text.splitlines()]
    types = [event["type"] for event in events]
    assert types == ["answer_start", "stage", "claim", "final"]
    claim = events[2]
    assert claim["claim_index"] == 0
    assert claim["text"] == "已验证答案"
    assert claim["supports"][0]["quote"] == "证据原文"
    assert "raw_output" not in response.text
    assert "delta" not in response.text


def test_query_and_admin_tokens_are_not_interchangeable(
    tmp_path: Path,
) -> None:
    client, query_token, admin_token = _client(tmp_path)

    assert client.post(
        "/api/chat",
        headers=_bearer(admin_token),
        json={"conversation_id": "c", "question": "核验问题"},
    ).status_code == 401
    assert client.post(
        "/api/index/jobs",
        headers=_bearer(query_token),
        json={"idempotency_key": "sync-1", "kind": "incremental"},
    ).status_code == 401
    assert client.post(
        "/api/chat",
        json={"conversation_id": "c", "question": "核验问题"},
    ).status_code == 401


def test_admin_job_is_idempotent_and_status_is_readable(
    tmp_path: Path,
) -> None:
    client, _, admin_token = _client(tmp_path)
    request = {"idempotency_key": "sync-1", "kind": "incremental"}

    first = client.post(
        "/api/index/jobs",
        headers=_bearer(admin_token),
        json=request,
    )
    second = client.post(
        "/api/index/jobs",
        headers=_bearer(admin_token),
        json=request,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    status = client.get(
        f"/api/index/jobs/{first.json()['job_id']}",
        headers=_bearer(admin_token),
    )
    assert status.status_code == 200
    assert status.json()["state"] == "pending"


def test_clear_conversation_requires_query_token(tmp_path: Path) -> None:
    client, query_token, admin_token = _client(tmp_path)

    assert client.delete(
        "/api/conversations/conversation-1",
        headers=_bearer(admin_token),
    ).status_code == 401
    assert client.delete(
        "/api/conversations/conversation-1",
        headers=_bearer(query_token),
    ).status_code == 204


def test_feedback_requires_query_token_and_is_idempotent(
    tmp_path: Path,
) -> None:
    client, query_token, admin_token = _client(tmp_path)
    payload = {"trace_id": "a" * 32, "useful": True}

    assert client.post(
        "/api/feedback",
        headers=_bearer(admin_token),
        json=payload,
    ).status_code == 401
    assert client.post(
        "/api/feedback",
        headers=_bearer(query_token),
        json=payload,
    ).status_code == 204
    payload["useful"] = False
    assert client.post(
        "/api/feedback",
        headers=_bearer(query_token),
        json=payload,
    ).status_code == 204


def test_frontend_uses_only_local_assets_and_required_controls(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="question"' in response.text
    assert 'id="stages"' in response.text
    assert 'id="answer"' in response.text
    assert 'id="citations"' in response.text
    assert 'id="clear"' in response.text
    assert 'id="feedback-useful"' in response.text
    assert 'id="feedback-not-useful"' in response.text
    assert "https://" not in response.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200
