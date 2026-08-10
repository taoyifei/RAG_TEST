import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from rag_app.api.app import ApiServices, create_app
from rag_app.generation.answer import AnswerResult, AnswerStatus, RefusalCode
from rag_app.health import ComponentStatus, ReadinessService
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import QueryOutcome, StageEvent
from rag_app.settings import UiQueryAuthMode
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore


@dataclass(frozen=True, slots=True)
class _ReadyProbe:
    def check(self) -> ComponentStatus:
        return ComponentStatus("local", True, "ready", 1, 1)


class _Query:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def ask(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        del conversation_id, now, emit
        self.questions.append(question)
        return QueryOutcome(
            trace_id=trace_id,
            answer=AnswerResult(
                status=AnswerStatus.REFUSED,
                answer=None,
                claims=(),
                refusal_code=RefusalCode.NO_EVIDENCE,
                model_calls=0,
                calls=(),
            ),
            rewritten=False,
            stage_count=0,
        )

    def ask_stream(  # noqa: PLR0913
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
        emit_answer: object,
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


@dataclass(slots=True)
class _Context:
    client: TestClient
    query_token: str
    admin_token: str
    query: _Query
    executor: QueryExecutor
    readiness: ReadinessService

    def close(self) -> None:
        self.executor.close()
        self.readiness.close()


def _context(tmp_path: Path) -> _Context:
    query_token = "query-" + uuid.uuid4().hex
    admin_token = "admin-" + uuid.uuid4().hex
    state_path = tmp_path / "state.sqlite3"
    conversations = ConversationStore(
        state_path,
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    jobs = JobStore(state_path)
    jobs.initialize()
    feedback = FeedbackStore(state_path)
    feedback.initialize()
    readiness = ReadinessService((_ReadyProbe(),))
    readiness.refresh_once()
    executor = QueryExecutor()
    query = _Query()
    app = create_app(
        ApiServices(
            readiness=readiness,
            query_token=query_token,
            admin_token=admin_token,
            query=query,  # type: ignore[arg-type]
            query_executor=executor,
            conversations=conversations,
            jobs=jobs,
            feedback=feedback,
            pipeline_fingerprint="pipeline-1",
            frontend_dir=Path(__file__).parents[1] / "frontend",
            ui_query_auth_mode=UiQueryAuthMode.SAME_ORIGIN_SESSION,
        ui_cookie_secure=False,
            ui_session_ttl_seconds=300,
        )
    )
    return _Context(
        client=TestClient(app),
        query_token=query_token,
        admin_token=admin_token,
        query=query,
        executor=executor,
        readiness=readiness,
    )


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver", "Host": "testserver"}


def _session(context: _Context) -> tuple[str, str]:
    response = context.client.post("/api/ui/session", headers=_origin())
    assert response.status_code == 201
    return response.json()["csrf_token"], response.headers["set-cookie"]


def test_same_origin_frontend_has_no_query_token_control_or_secret(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    try:
        page = context.client.get("/")
        script = context.client.get("/assets/app.js")

        assert page.status_code == 200
        assert 'id="token"' not in page.text
        assert "查询令牌" not in page.text
        assert "tokenInput" not in script.text
        assert "authorization()" not in script.text
        assert "/api/ui/session" in script.text
        assert "/api/ui/chat" in script.text
        assert 'credentials: "same-origin"' in script.text
        assert context.query_token not in page.text
        assert context.query_token not in script.text
    finally:
        context.close()


def test_ui_session_cookie_and_chat_are_bounded_and_token_free(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    try:
        csrf_token, cookie = _session(context)

        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/api/ui/" in cookie
        assert "Max-Age=300" in cookie
        assert context.query_token not in cookie
        assert context.query_token not in csrf_token

        response = context.client.post(
            "/api/ui/chat",
            headers={**_origin(), "X-CSRF-Token": csrf_token},
            json={"conversation_id": "ui-1", "question": "核验问题"},
        )

        assert response.status_code == 200
        assert context.query.questions == ["核验问题"]
        assert context.query_token not in response.text
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == "final"
    finally:
        context.close()


def test_ui_session_rejects_missing_csrf_bad_origin_and_forgery(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    try:
        csrf_token, _ = _session(context)
        payload = {"conversation_id": "ui-1", "question": "核验问题"}

        missing = context.client.post(
            "/api/ui/chat",
            headers=_origin(),
            json=payload,
        )
        wrong = context.client.post(
            "/api/ui/chat",
            headers={**_origin(), "X-CSRF-Token": "wrong"},
            json=payload,
        )
        bad_origin = context.client.post(
            "/api/ui/chat",
            headers={
                "Origin": "http://attacker.invalid",
                "Host": "testserver",
                "X-CSRF-Token": csrf_token,
            },
            json=payload,
        )
        context.client.cookies.set(
            "rag_ui_session",
            "forged",
            path="/api/ui/",
        )
        forged = context.client.post(
            "/api/ui/chat",
            headers={**_origin(), "X-CSRF-Token": csrf_token},
            json=payload,
        )

        assert missing.status_code == 403
        assert wrong.status_code == 403
        assert bad_origin.status_code == 403
        assert forged.status_code == 401
        assert context.query.questions == []
    finally:
        context.close()


def test_ui_session_clear_feedback_and_admin_isolation(tmp_path: Path) -> None:
    context = _context(tmp_path)
    try:
        csrf_token, _ = _session(context)
        headers = {**_origin(), "X-CSRF-Token": csrf_token}

        assert context.client.delete(
            "/api/ui/conversations/ui-1",
            headers=headers,
        ).status_code == 204
        assert context.client.post(
            "/api/ui/feedback",
            headers=headers,
            json={"trace_id": "a" * 32, "useful": True},
        ).status_code == 204
        assert context.client.get(
            "/api/admin/traces",
            headers=_origin(),
        ).status_code in {401, 503}
        context.client.cookies.clear()
        assert context.client.post(
            "/api/ui/chat",
            headers={
                **_origin(),
                "Authorization": f"Bearer {context.admin_token}",
                "X-CSRF-Token": "missing-session",
            },
            json={"conversation_id": "ui-1", "question": "核验问题"},
        ).status_code == 401
    finally:
        context.close()


def test_external_chat_bearer_contract_is_unchanged(tmp_path: Path) -> None:
    context = _context(tmp_path)
    try:
        payload = {"conversation_id": "api-1", "question": "核验问题"}

        assert context.client.post("/api/chat", json=payload).status_code == 401
        assert context.client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {context.admin_token}"},
            json=payload,
        ).status_code == 401
        assert context.client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {context.query_token}"},
            json=payload,
        ).status_code == 200
    finally:
        context.close()
