import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from rag_app.api.app import ApiServices, create_app
from rag_app.generation.answer import (
    AnswerResult,
    AnswerStatus,
    RefusalCode,
)
from rag_app.health import ComponentStatus, ReadinessService
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import QueryOutcome, StageEvent
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore
from rag_app.tracing.models import (
    TraceIdentity,
    TraceMode,
    TraceRecord,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import TraceRecorder
from rag_app.tracing.store import TraceStore


@dataclass(frozen=True, slots=True)
class _ReadyProbe:
    def check(self) -> ComponentStatus:
        return ComponentStatus("local", True, "ready", 1, 1)


class _Query:
    def __init__(self) -> None:
        self.debug_calls = 0

    def ask(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        del conversation_id, question, now, emit
        return _outcome(trace_id)

    def ask_debug(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        del conversation_id, question, now, emit
        self.debug_calls += 1
        return _outcome(trace_id)


@dataclass(slots=True)
class _ApiContext:
    client: TestClient
    query_token: str
    admin_token: str
    store: TraceStore
    recorder: TraceRecorder
    executor: QueryExecutor
    readiness: ReadinessService
    query: _Query

    def close(self) -> None:
        self.executor.close()
        self.recorder.close()
        self.readiness.close()


@pytest.fixture
def trace_api(tmp_path: Path) -> Iterator[_ApiContext]:
    query_token = uuid.uuid4().hex
    admin_token = uuid.uuid4().hex
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
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
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
            trace_store=store,
            trace_recorder=recorder,
        )
    )
    context = _ApiContext(
        client=TestClient(app),
        query_token=query_token,
        admin_token=admin_token,
        store=store,
        recorder=recorder,
        executor=executor,
        readiness=readiness,
        query=query,
    )
    try:
        yield context
    finally:
        context.close()


def _trace(
    trace_id: str,
    *,
    mode: TraceMode = TraceMode.FULL,
    created_at: datetime | None = None,
) -> TraceRecord:
    created = created_at or datetime.now(UTC)
    ttl = timedelta(hours=72) if mode is TraceMode.FULL else timedelta(days=30)
    return TraceRecord(
        trace_id=trace_id,
        schema_version="1",
        mode=mode,
        created_at=created,
        finished_at=None,
        duration_ms=None,
        pipeline_fingerprint="sha256:" + "1" * 64,
        serving_fingerprint="sha256:" + "2" * 64,
        release_revision="release-1",
        active_collection="rag-active-v1",
        index_manifest_sha256="3" * 64,
        payload_schema_version=2,
        status=TraceStatus.RUNNING,
        refusal_code=None,
        error_code=None,
        feedback_useful=None,
        capture_complete=True,
        expires_at=created + ttl,
    )


def _outcome(trace_id: str) -> QueryOutcome:
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_query_token_cannot_access_any_trace_admin_api(
    trace_api: _ApiContext,
) -> None:
    trace_id = "a" * 32
    paths = (
        "/api/admin/traces",
        f"/api/admin/traces/{trace_id}",
        f"/api/admin/traces/{trace_id}/artifacts/{'b' * 32}",
        f"/api/admin/traces/{trace_id}/export",
    )

    for path in paths:
        assert trace_api.client.get(
            path,
            headers=_bearer(trace_api.query_token),
        ).status_code == 401
    assert trace_api.client.post(
        "/api/admin/traces/export",
        headers=_bearer(trace_api.query_token),
        json={"trace_ids": [trace_id]},
    ).status_code == 401
    assert trace_api.client.post(
        "/api/admin/debug/chat",
        headers=_bearer(trace_api.query_token),
        json={"conversation_id": "c", "question": "公开合成问题"},
    ).status_code == 401


def test_admin_list_detail_artifact_and_export_are_no_store(
    trace_api: _ApiContext,
) -> None:
    trace_id = "a" * 32
    trace_api.store.create_trace(_trace(trace_id))
    artifact = trace_api.store.add_artifact(
        trace_id,
        kind="context",
        media_type="application/json",
        payload=b'{"question":"public synthetic question"}',
    )
    headers = _bearer(trace_api.admin_token)

    listed = trace_api.client.get(
        "/api/admin/traces?page=1&page_size=10&status=RUNNING",
        headers=headers,
    )
    detail = trace_api.client.get(
        f"/api/admin/traces/{trace_id}",
        headers=headers,
    )
    content = trace_api.client.get(
        f"/api/admin/traces/{trace_id}/artifacts/{artifact.artifact_id}",
        headers=headers,
    )
    exported = trace_api.client.get(
        f"/api/admin/traces/{trace_id}/export",
        headers=headers,
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["trace_id"] == trace_id
    assert detail.status_code == 200
    assert detail.json()["trace"]["mode"] == "FULL"
    assert detail.json()["artifacts"][0]["artifact_id"] == artifact.artifact_id
    assert "payload" not in detail.json()["artifacts"][0]
    assert content.status_code == 200
    assert content.json()["question"] == "public synthetic question"
    assert exported.status_code == 200
    assert json.loads(exported.content)["trace"]["trace_id"] == trace_id
    assert exported.headers["content-disposition"] == (
        f'attachment; filename="{trace_id}.json"'
    )
    for response in (listed, detail, content, exported):
        assert response.headers["cache-control"] == "no-store"


def test_admin_can_batch_export_selected_traces_as_zip(
    trace_api: _ApiContext,
) -> None:
    first_id = "a" * 32
    second_id = "b" * 32
    trace_api.store.create_trace(_trace(first_id))
    trace_api.store.create_trace(_trace(second_id))

    response = trace_api.client.post(
        "/api/admin/traces/export",
        headers=_bearer(trace_api.admin_token),
        json={"trace_ids": [second_id, first_id]},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="rag-traces.zip"'
    )
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            f"{second_id}.json",
            f"{first_id}.json",
            "TRACE_EXPORT_MANIFEST.json",
        ]
        assert json.loads(archive.read(f"{second_id}.json"))["trace"][
            "trace_id"
        ] == second_id
        assert json.loads(archive.read(f"{first_id}.json"))["trace"][
            "trace_id"
        ] == first_id
        manifest = json.loads(archive.read("TRACE_EXPORT_MANIFEST.json"))

    assert manifest == {
        "traces": [
            {
                "trace_id": second_id,
                "json_file": f"{second_id}.json",
                "json_sha256": hashlib.sha256(
                    trace_api.store.export_trace(second_id)
                ).hexdigest(),
                "created_at": trace_api.store.get_trace(
                    second_id
                ).trace.created_at.isoformat(),
                "status": "RUNNING",
                "question_sha256": None,
            },
            {
                "trace_id": first_id,
                "json_file": f"{first_id}.json",
                "json_sha256": hashlib.sha256(
                    trace_api.store.export_trace(first_id)
                ).hexdigest(),
                "created_at": trace_api.store.get_trace(
                    first_id
                ).trace.created_at.isoformat(),
                "status": "RUNNING",
                "question_sha256": None,
            },
        ]
    }


def test_batch_export_rejects_empty_duplicate_or_missing_trace(
    trace_api: _ApiContext,
) -> None:
    trace_id = "a" * 32
    trace_api.store.create_trace(_trace(trace_id))
    headers = _bearer(trace_api.admin_token)

    assert trace_api.client.post(
        "/api/admin/traces/export",
        headers=headers,
        json={"trace_ids": []},
    ).status_code == 422
    assert trace_api.client.post(
        "/api/admin/traces/export",
        headers=headers,
        json={"trace_ids": [trace_id, trace_id]},
    ).status_code == 422
    assert trace_api.client.post(
        "/api/admin/traces/export",
        headers=headers,
        json={"trace_ids": ["b" * 32]},
    ).status_code == 404
    assert trace_api.client.post(
        "/api/admin/traces/export",
        headers=headers,
        json={"trace_ids": [f"{index:032x}" for index in range(101)]},
    ).status_code == 422


def test_batch_export_records_safe_question_hash_without_question_body(
    trace_api: _ApiContext,
) -> None:
    trace_id = "c" * 32
    question = "快验还是产品开发"
    session = trace_api.recorder.begin_query(
        trace_id,
        TraceMode.SAFE,
        datetime.now(UTC),
        TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="release-1",
            active_collection="rag-active-v1",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
        ),
        question_sha256=hashlib.sha256(question.encode("utf-8")).hexdigest(),
    )
    session.finish(
        status=TraceStatus.ANSWERED,
        reason_code=DecisionCode.ANSWERED,
    )
    trace_api.recorder.flush()

    response = trace_api.client.post(
        "/api/admin/traces/export",
        headers=_bearer(trace_api.admin_token),
        json={"trace_ids": [trace_id]},
    )

    assert response.status_code == 200
    assert question.encode("utf-8") not in response.content
    with ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("TRACE_EXPORT_MANIFEST.json"))

    assert manifest["traces"][0]["question_sha256"] == hashlib.sha256(
        question.encode("utf-8")
    ).hexdigest()


def test_artifact_cannot_cross_trace_and_expired_returns_410(
    trace_api: _ApiContext,
) -> None:
    first_id = "a" * 32
    second_id = "b" * 32
    expired_id = "c" * 32
    trace_api.store.create_trace(_trace(first_id))
    trace_api.store.create_trace(_trace(second_id))
    trace_api.store.create_trace(
        _trace(
            expired_id,
            created_at=datetime.now(UTC) - timedelta(hours=73),
        )
    )
    first_artifact = trace_api.store.add_artifact(
        first_id,
        kind="context",
        media_type="application/json",
        payload=b'{"value":1}',
    )
    expired_artifact = trace_api.store.add_artifact(
        expired_id,
        kind="context",
        media_type="application/json",
        payload=b'{"value":2}',
    )
    headers = _bearer(trace_api.admin_token)

    assert trace_api.client.get(
        (
            f"/api/admin/traces/{second_id}/artifacts/"
            f"{first_artifact.artifact_id}"
        ),
        headers=headers,
    ).status_code == 404
    assert trace_api.client.get(
        (
            f"/api/admin/traces/{expired_id}/artifacts/"
            f"{expired_artifact.artifact_id}"
        ),
        headers=headers,
    ).status_code == 410


def test_full_debug_store_failure_is_503_before_query_executes(
    trace_api: _ApiContext,
) -> None:
    trace_api.store.close()

    response = trace_api.client.post(
        "/api/admin/debug/chat",
        headers=_bearer(trace_api.admin_token),
        json={"conversation_id": "c", "question": "公开合成问题"},
    )

    assert response.status_code == 503
    assert trace_api.query.debug_calls == 0


def test_debug_page_uses_only_local_assets(trace_api: _ApiContext) -> None:
    page = trace_api.client.get("/debug/")
    script = trace_api.client.get("/assets/debug.js")

    assert page.status_code == 200
    assert script.status_code == 200
    assert "https://" not in page.text
    assert "innerHTML" not in script.text
    assert "/api/admin/traces" in script.text
