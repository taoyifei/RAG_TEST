"""F05 有界快照、计数、失败降级和管理员接口验收。"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.question_analytics import (
    QuestionAnalyticsService,
    normalize_question,
)
from tests.wanshitong.support import PublicHarness, synthetic_answer


def _scope(harness: PublicHarness) -> KnowledgeBaseScope:
    binding = harness.scope_service.binding()
    return KnowledgeBaseScope(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
    )


def _service(harness: PublicHarness) -> QuestionAnalyticsService:
    runtime = harness.product.runtime
    return QuestionAnalyticsService(
        runtime.connections,
        runtime.history,
        deployment_id="candidate_8289",
        retention_days=runtime.settings.history_retention_days,
    )


def _add_request(  # noqa: PLR0913
    harness: PublicHarness,
    question: str,
    *,
    owner_id: str,
    deployment_id: str = "candidate_8289",
    traffic_class: str = "INTERACTIVE",
    entrypoint: str = "manual",
    status: str = "ANSWERED",
    save_body: bool = True,
    context_digest: str | None = None,
) -> str:
    trace_id = "trace_" + uuid.uuid4().hex
    scope = _scope(harness)
    audit = QueryAuditContext(
        trace_id=trace_id,
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        owner_id=owner_id,
        deployment_id=deployment_id,
        identity_source="RDMS_SSO",
        traffic_class=traffic_class,  # type: ignore[arg-type]
        classification_source="PUBLIC_ENDPOINT",
        entrypoint=entrypoint,  # type: ignore[arg-type]
    )
    history = harness.product.runtime.history
    history.start(
        trace_id,
        scope,
        question,
        owner_id=owner_id,
        save_body=save_body,
        audit_context=audit,
        conversation_context_digest=context_digest,
    )
    if status == "ANSWERED":
        history.finish(
            trace_id,
            result=synthetic_answer(trace_id, include_evidence=False),
            error=None,
            cancelled=False,
        )
    elif status == "FAILED":
        history.finish(trace_id, result=None, error=None, cancelled=False)
    elif status == "CANCELLED":
        history.finish(trace_id, result=None, error=None, cancelled=True)
    else:
        assert status == "STARTED"
    return trace_id


def _complete_run(
    service: QuestionAnalyticsService, scope: KnowledgeBaseScope
) -> dict[str, object]:
    started = service.refresh(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = service.run(
            str(started["run_id"]),
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
        )
        if run["state"] != "BUILDING":
            return run
        time.sleep(0.02)
    pytest.fail("统计后台任务未在十秒内完成")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("3个工作日", "3个自然日"),
        ("可以办理", "必须办理"),
        ("经理", "经理助理"),
        ("2025版", "2026版"),
        ("OPC", "opc"),
        ("a b", "ab"),
    ],
)
def test_normalizer_keeps_meaningful_differences(left: str, right: str) -> None:
    assert normalize_question(left) != normalize_question(right)
    assert normalize_question("  咨询\t事项？  ") == normalize_question(
        "咨询 事项?"
    )


def test_f05_counts_latest_feedback_and_preserves_previous_run(  # noqa: PLR0915
    public_harness: PublicHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _scope(public_harness)
    service = _service(public_harness)
    first_trace = ""
    for index in range(20):
        trace_id = _add_request(
            public_harness, "如何办理？", owner_id="rdms:1001"
        )
        if index == 0:
            first_trace = trace_id
    second = _add_request(public_harness, "如何办理?", owner_id="rdms:1002")
    for _ in range(3):
        _add_request(
            public_harness,
            "如何办理？",
            owner_id="rdms:test",
            traffic_class="EVALUATION",
        )
    _add_request(
        public_harness,
        "如何办理？",
        owner_id="rdms:1001",
        status="CANCELLED",
    )
    _add_request(
        public_harness,
        "如何办理？",
        owner_id="rdms:1001",
        status="STARTED",
    )
    _add_request(
        public_harness,
        "如何办理？",
        owner_id="rdms:1001",
        save_body=False,
    )
    _add_request(
        public_harness,
        "如何办理？",
        owner_id="rdms:production",
        deployment_id="production_18288",
    )
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE query_history SET created_at=? WHERE trace_id=?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), first_trace),
        )
    now = datetime.now(UTC).isoformat()
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "INSERT INTO product_feedback(trace_id, project_id, "
            "knowledge_base_id, owner_id, useful, reason_code, "
            "projection_state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 'INCORRECT', 'APPLIED', ?, ?)",
            (
                second,
                scope.project_id,
                scope.knowledge_base_id,
                "rdms:1002",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO wanshitong_feedback_details(trace_id, reason_detail, "
            "feedback_revision, updated_at) VALUES (?, 'FALSE_REFUSAL', 2, ?)",
            (second, now),
        )
        connection.execute(
            "INSERT INTO wanshitong_feedback_reviews(trace_id, review_status, "
            "root_cause, reviewed_feedback_revision, "
            "reviewed_by_admin, updated_at) "
            "VALUES (?, 'REVIEWED', 'FALSE_REFUSAL', 2, 'admin', ?)",
            (second, now),
        )
    first = _complete_run(service, scope)
    assert first["state"] == "COMPLETE", first
    assert first["source_total"] == 27
    assert first["eligible_count"] == 21
    assert first["excluded_count"] == 4
    assert first["pending_count"] == 1
    assert first["body_missing_count"] == 1
    frequent = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )
    item = frequent["items"][0]
    assert item["request_count"] == 21
    assert item["distinct_users"] == 2
    assert item["user_day_heat"] == 3
    assert item["manual_request_count"] == 21
    assert item["answered_count"] == 21
    assert item["feedback_count"] == 1
    assert item["helpful_count"] == 0
    assert item["negative_feedback_count"] == 1
    assert item["false_refusal_count"] == 1
    assert item["confirmed_open_issue_count"] == 1
    assert item["representative_question"] is not None
    samples = service.samples(
        run_id=str(first["run_id"]),
        group_key=str(item["group_key"]),
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
    )
    assert len(samples["items"]) <= 3

    # 通用反馈入口只改 canonical，旧湾事通详情和复核不可继续计数。
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE product_feedback SET reason_code='INCOMPLETE', "
            "updated_at=? WHERE trace_id=?",
            (datetime.now(UTC).isoformat(), second),
        )
    stale = _complete_run(service, scope)
    assert stale["state"] == "COMPLETE"
    stale_item = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )["items"][0]
    assert stale_item["negative_feedback_count"] == 1
    assert stale_item["false_refusal_count"] == 0
    assert stale_item["confirmed_open_issue_count"] == 0

    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE product_feedback SET useful=1, reason_code=NULL, "
            "updated_at=? WHERE trace_id=?",
            (datetime.now(UTC).isoformat(), second),
        )
        connection.execute(
            "UPDATE wanshitong_feedback_details SET reason_detail=NULL, "
            "feedback_revision=3 WHERE trace_id=?",
            (second,),
        )
    updated = _complete_run(service, scope)
    assert updated["state"] == "COMPLETE"
    updated_board = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )
    updated_item = updated_board["items"][0]
    assert updated_item["request_count"] == item["request_count"]
    assert updated_item["distinct_users"] == item["distinct_users"]
    assert updated_item["helpful_count"] == 1
    assert updated_item["confirmed_open_issue_count"] == 0

    monkeypatch.setattr("rag_app.wanshitong.question_analytics._MAX_ROWS", 1)
    limited = _complete_run(service, scope)
    assert limited["state"] == "LIMITED"
    previous = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )
    assert previous["run"]["run_id"] == updated["run_id"]
    assert previous["latest_run"]["run_id"] == limited["run_id"]


def test_context_scope_and_expired_samples(
    public_harness: PublicHarness,
) -> None:
    scope = _scope(public_harness)
    service = _service(public_harness)
    exact = _add_request(public_harness, "那多久？", owner_id="rdms:1001")
    _add_request(
        public_harness,
        "那多久？",
        owner_id="rdms:1001",
        context_digest="1" * 64,
    )
    _add_request(
        public_harness,
        "那多久？",
        owner_id="rdms:1001",
        context_digest="2" * 64,
    )
    run = _complete_run(service, scope)
    assert run["state"] == "COMPLETE"
    board = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )
    assert len(board["items"]) == 3
    assert {item["group_kind"] for item in board["items"]} == {
        "EXACT",
        "CONTEXT",
    }
    exact_item = next(
        item for item in board["items"] if item["group_kind"] == "EXACT"
    )
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE query_history SET expires_at=? WHERE trace_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), exact),
        )
    assert (
        service.samples(
            run_id=str(run["run_id"]),
            group_key=str(exact_item["group_key"]),
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
        )["items"]
        == []
    )


def test_refresh_requires_admin_csrf(public_harness: PublicHarness) -> None:
    path = ADMIN_BASE_PATH + "/question-analytics:refresh"
    assert public_harness.client.post(path).status_code == 403
    response = public_harness.client.post(
        path, headers=public_harness.product.write_headers
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert (
        public_harness.client.get(
            ADMIN_BASE_PATH + f"/question-analytics/runs/{run_id}"
        ).status_code
        == 200
    )


def test_source_change_before_publish_marks_run_limited(
    public_harness: PublicHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _scope(public_harness)
    service = _service(public_harness)
    trace_id = _add_request(public_harness, "申请条件？", owner_id="rdms:1")
    baseline = _complete_run(service, scope)
    assert baseline["state"] == "COMPLETE"
    original_calculate = service._calculate

    def change_source(
        rows: list[dict[str, object]], run: dict[str, object]
    ) -> tuple[object, object]:
        result = original_calculate(rows, run)
        with public_harness.product.runtime.connections.transaction(
            write=True
        ) as connection:
            connection.execute(
                "UPDATE query_history SET created_at=? WHERE trace_id=?",
                ((datetime.now(UTC) - timedelta(days=8)).isoformat(), trace_id),
            )
        return result

    monkeypatch.setattr(service, "_calculate", change_source)
    changed = _complete_run(service, scope)
    assert changed["state"] == "LIMITED"
    assert changed["failure_code"] == "SOURCE_OR_CLASSIFICATION_CHANGED"
    board = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )
    assert board["run"]["run_id"] == baseline["run_id"]


def test_history_terminal_statuses_are_counted_separately(
    public_harness: PublicHarness,
) -> None:
    scope = _scope(public_harness)
    service = _service(public_harness)
    refused = _add_request(
        public_harness, "不能办理？", owner_id="rdms:1", status="STARTED"
    )
    public_harness.product.runtime.history.finish(
        refused,
        result=synthetic_answer(refused, include_evidence=False).model_copy(
            update={"answer": ""}
        ),
        error=None,
        cancelled=False,
    )
    _add_request(
        public_harness, "不能办理？", owner_id="rdms:1", status="FAILED"
    )
    interrupted = _add_request(
        public_harness, "不能办理？", owner_id="rdms:1", status="STARTED"
    )
    _add_request(
        public_harness, "不能办理？", owner_id="rdms:1", status="CANCELLED"
    )
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "UPDATE query_history SET status='INTERRUPTED' WHERE trace_id=?",
            (interrupted,),
        )
    run = _complete_run(service, scope)
    assert run["state"] == "COMPLETE"
    assert run["source_total"] == 4
    assert run["eligible_count"] == 3
    assert run["excluded_count"] == 1
    item = service.board(
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        board="frequent",
    )["items"][0]
    assert item["request_count"] == 3
    assert item["answered_count"] == 0
    assert item["refused_count"] == 1
    assert item["failed_count"] == 2
