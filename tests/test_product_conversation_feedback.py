"""Product Conversation 与 canonical Feedback 的范围和恢复回归。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import NotFound, PolicyDenied, QueryCancelled
from rag_app.core.identifiers import new_id
from rag_app.core.models import KnowledgeBaseScope
from rag_app.product.conversations import ProductConversationStore
from rag_app.product.crypto import SecretCipher, load_master_key
from rag_app.product.feedback import ProductFeedbackStore
from rag_app.tracing.store import TraceNotFoundError
from tests.api.test_query_history import _upload
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)


def test_sdk_conversation_sync_stream_commit_and_cancel(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        first = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            "MX-41",
            owner_id="owner-a",
            trace_id=new_id("trace"),
            conversation_id="conversation-sdk",
        )
        events: list[object] = []
        streamed = harness.runtime.sdk.answer_stream(
            project_id,
            knowledge_base_id,
            "MX-41",
            emit=events.append,
            cancellation=StreamCancellation(),
            owner_id="owner-a",
            trace_id=new_id("trace"),
            conversation_id="conversation-sdk",
        )
        assert streamed.answer is not None
        assert events
        with harness.runtime.connections.transaction() as connection:
            rows = connection.execute(
                "SELECT turn_id FROM product_conversation_turns "
                "WHERE conversation_id='conversation-sdk' ORDER BY ordinal"
            ).fetchall()
        assert [str(row[0]) for row in rows] == [
            first.trace_id,
            streamed.trace_id,
        ]
        detail = harness.runtime.history.detail(streamed.trace_id)
        assert detail["conversation_context_present"] is True
        assert len(str(detail["conversation_context_digest"])) == 64

        cancelled = StreamCancellation()
        cancelled.cancel()
        cancelled_trace = new_id("trace")
        with pytest.raises(QueryCancelled):
            harness.runtime.sdk.answer_stream(
                project_id,
                knowledge_base_id,
                "取消请求",
                emit=lambda _: None,
                cancellation=cancelled,
                owner_id="owner-a",
                trace_id=cancelled_trace,
                conversation_id="conversation-sdk",
            )
        with harness.runtime.connections.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM product_conversation_turns WHERE turn_id=?",
                    (cancelled_trace,),
                ).fetchone()
                is None
            )
    finally:
        harness.close()


def test_feedback_and_conversation_http_contract(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        endpoint = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer"
        )
        answer = harness.client.post(
            endpoint,
            headers=harness.write_headers,
            json={"query": "MX-41", "conversation_id": "browser-session"},
        )
        assert answer.status_code == 200, answer.text
        trace_id = str(answer.json()["trace_id"])
        feedback_path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/queries/{trace_id}/feedback"
        )
        saved = harness.client.put(
            feedback_path,
            headers=harness.write_headers,
            json={"useful": False, "reason_code": "INCOMPLETE"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["projection_state"] == "APPLIED"
        assert (
            harness.client.get(feedback_path).json()["feedback"]["reason_code"]
            == "INCOMPLETE"
        )

        clear_path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/conversations/browser-session"
        )
        cleared = harness.client.delete(
            clear_path,
            headers=harness.write_headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["deleted_turns"] == 1
    finally:
        harness.close()


def test_conversation_is_encrypted_scoped_idempotent_and_revocable(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        document_id = _upload(harness, project_id, knowledge_base_id)
        store = ProductConversationStore(
            harness.runtime.connections,
            SecretCipher(load_master_key(tmp_path / "master-key")),
        )
        scope = KnowledgeBaseScope(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        question = "MX-41 的维护周期是多少？"
        result = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            question,
            owner_id="owner-a",
            trace_id=new_id("trace"),
        )

        assert store.commit(
            scope,
            "conversation-1",
            question,
            result,
            owner_id="owner-a",
        )
        assert not store.commit(
            scope,
            "conversation-1",
            question,
            result,
            owner_id="owner-a",
        )
        context = store.context(scope, "conversation-1", owner_id="owner-a")
        assert len(context) == 1
        assert question in context[0]
        assert "14 天" in context[0]
        assert store.context(scope, "conversation-1", owner_id="owner-b") == ()
        assert question.encode() not in (
            harness.runtime.connections.database_path.read_bytes()
        )
        fallback_question = "请继续说明这个周期。"
        fallback = result.model_copy(
            update={
                "trace_id": new_id("trace"),
                "generation_mode": "extractive_fallback",
                "generation_reason_code": "PROVIDER_TIMEOUT",
                "degraded_reason_codes": ("PROVIDER_TIMEOUT",),
            }
        )
        assert store.commit(
            scope,
            "conversation-fallback",
            fallback_question,
            fallback,
            owner_id="owner-a",
        )
        fallback_context = store.context(
            scope,
            "conversation-fallback",
            owner_id="owner-a",
        )
        assert len(fallback_context) == 1
        assert fallback_question in fallback_context[0]
        assert "14 天" in fallback_context[0]

        harness.runtime.sdk.delete_document(
            project_id,
            knowledge_base_id,
            document_id,
        )
        revoked_context = store.context(
            scope, "conversation-1", owner_id="owner-a"
        )
        assert len(revoked_context) == 1
        assert question in revoked_context[0]
        assert "14 天" not in revoked_context[0]
        cleared = store.clear(scope, "conversation-1", owner_id="owner-a")
        assert cleared.deleted
        assert cleared.deleted_turns == 1
    finally:
        harness.close()


def test_conversation_serializes_duplicate_final_and_expires(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        store = ProductConversationStore(
            harness.runtime.connections,
            SecretCipher(load_master_key(tmp_path / "master-key")),
        )
        scope = KnowledgeBaseScope(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        result = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            "MX-41",
            owner_id="owner-a",
            trace_id=new_id("trace"),
        )

        def commit() -> bool:
            with store.lease(scope, "conversation-race", owner_id="owner-a"):
                return store.commit(
                    scope,
                    "conversation-race",
                    "MX-41",
                    result,
                    owner_id="owner-a",
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(lambda _: commit(), range(2)))
        assert sorted(outcomes) == [False, True]
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE product_conversations SET expires_at=?",
                ("2000-01-01T00:00:00+00:00",),
            )
        assert (
            store.context(scope, "conversation-race", owner_id="owner-a") == ()
        )
    finally:
        harness.close()


def test_feedback_upsert_scope_legacy_id_and_recovery(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        result = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            "MX-41",
            owner_id="owner-a",
            trace_id=new_id("trace"),
        )
        projected: list[tuple[str, bool]] = []

        def project(trace_id: str, *, useful: bool) -> bool:
            projected.append((trace_id, useful))
            return harness.runtime.traces.set_feedback(trace_id, useful=useful)

        store = ProductFeedbackStore(
            harness.runtime.connections,
            project,
        )
        with pytest.raises(PolicyDenied):
            store.upsert(
                result.trace_id,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                actor_owner_id="owner-b",
                actor_is_admin=False,
                useful=False,
                reason_code="WRONG_SOURCE",
            )

        feedback = store.upsert(
            result.trace_id.removeprefix("trace_"),
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id="owner-a",
            actor_is_admin=False,
            useful=False,
            reason_code="WRONG_SOURCE",
        )
        assert feedback.trace_id == result.trace_id
        assert feedback.projection_state == "APPLIED"
        assert projected[-1] == (result.trace_id, False)
        assert (
            harness.runtime.traces.detail(result.trace_id).trace.feedback_useful
            is False
        )

        changed = store.upsert(
            result.trace_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id="local-admin",
            actor_is_admin=True,
            useful=True,
            reason_code=None,
        )
        assert changed.useful
        assert changed.projection_state == "APPLIED"

        with pytest.raises(NotFound):
            store.upsert(
                new_id("trace"),
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                actor_owner_id="local-admin",
                actor_is_admin=True,
                useful=True,
                reason_code=None,
            )
    finally:
        harness.close()


def test_feedback_projection_failure_is_recoverable(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        result = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            "MX-41",
            owner_id="owner-a",
            trace_id=new_id("trace"),
        )
        unavailable = True

        def project(trace_id: str, *, useful: bool) -> bool:
            if unavailable:
                raise TraceNotFoundError(trace_id)
            return harness.runtime.traces.set_feedback(trace_id, useful=useful)

        store = ProductFeedbackStore(harness.runtime.connections, project)
        pending = store.upsert(
            result.trace_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id="owner-a",
            actor_is_admin=False,
            useful=False,
            reason_code="OTHER",
        )
        assert pending.projection_state == "PENDING"
        unavailable = False
        assert store.recover() == 1
        recovered = store.get(
            result.trace_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id="owner-a",
            actor_is_admin=False,
        )
        assert recovered is not None
        assert recovered.projection_state == "APPLIED"
    finally:
        harness.close()


def test_feedback_projects_actual_bare_legacy_history_id(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        result = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            "MX-41",
            owner_id="owner-a",
            trace_id=new_id("trace"),
        )
        legacy_id = result.trace_id.removeprefix("trace_")
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM query_trace_events WHERE trace_id=?",
                (result.trace_id,),
            )
            connection.execute(
                "UPDATE query_history SET trace_id=? WHERE trace_id=?",
                (legacy_id, result.trace_id),
            )
        projected: list[str] = []

        def project(trace_id: str, *, useful: bool) -> bool:
            projected.append(trace_id)
            return harness.runtime.traces.set_feedback(trace_id, useful=useful)

        store = ProductFeedbackStore(harness.runtime.connections, project)
        feedback = store.upsert(
            legacy_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id="owner-a",
            actor_is_admin=False,
            useful=False,
            reason_code="OTHER",
        )

        assert projected == [legacy_id]
        assert feedback.trace_id == result.trace_id
        assert feedback.projection_state == "APPLIED"
    finally:
        harness.close()


def test_feedback_requeues_stale_projection_after_concurrent_upsert(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        result = harness.runtime.sdk.answer(
            project_id,
            knowledge_base_id,
            "MX-41",
            owner_id="owner-a",
            trace_id=new_id("trace"),
        )
        injected = False
        projected: list[bool] = []
        store: ProductFeedbackStore

        def project(trace_id: str, *, useful: bool) -> bool:
            nonlocal injected
            if not injected:
                injected = True
                store.upsert(
                    trace_id,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    actor_owner_id="owner-a",
                    actor_is_admin=False,
                    useful=True,
                    reason_code=None,
                )
            projected.append(useful)
            harness.runtime.traces.set_feedback(trace_id, useful=useful)
            return True

        store = ProductFeedbackStore(harness.runtime.connections, project)
        feedback = store.upsert(
            result.trace_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id="owner-a",
            actor_is_admin=False,
            useful=False,
            reason_code="OTHER",
        )

        assert feedback.useful is True
        assert feedback.projection_state == "APPLIED"
        assert projected[-1] is True
        assert (
            harness.runtime.traces.detail(result.trace_id).trace.feedback_useful
            is True
        )
    finally:
        harness.close()
