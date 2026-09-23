"""湾事通管理员提问者 ID 关联与隔离回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from rag_app.core.models import KnowledgeBaseScope
from rag_app.product.feedback import normalize_trace_id
from rag_app.tracing.models import TraceIdentity, TraceMode, TraceStatus
from rag_app.tracing.reasons import DecisionCode
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.public_api import PUBLIC_CHAT_PATH
from tests.wanshitong.support import PublicHarness, synthetic_answer


def _scope(harness: PublicHarness) -> KnowledgeBaseScope:
    binding = harness.scope_service.binding()
    return KnowledgeBaseScope(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
    )


def _history(
    harness: PublicHarness,
    suffix: str,
    owner_id: str,
    *,
    cache_hit: bool = False,
) -> str:
    trace_id = "trace_" + suffix * 32
    runtime = harness.product.runtime
    runtime.history.start(
        trace_id,
        _scope(harness),
        f"用户 {owner_id} 的问题 {suffix}",
        owner_id=owner_id,
        save_body=True,
    )
    answer = synthetic_answer(trace_id, include_evidence=False).model_copy(
        update={
            "answer": "已确认的完整答案。" * 35,
            "cache_hit": cache_hit,
            "result_origin": "cache" if cache_hit else "fresh",
        }
    )
    runtime.history.finish(
        trace_id, result=answer, error=None, cancelled=False
    )
    return trace_id


def _trace(harness: PublicHarness, suffix: str) -> str:
    trace_id = "trace_" + suffix * 32
    scope = _scope(harness)
    session = harness.product.runtime.traces.recorder.begin_query(
        trace_id,
        TraceMode.SAFE,
        datetime.now(UTC),
        TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="requester-test",
            active_collection="synthetic",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
        ),
    )
    session.finish(
        status=TraceStatus.SUCCEEDED,
        reason_code=DecisionCode.ANSWERED,
    )
    harness.product.runtime.traces.flush()
    return trace_id


def test_admin_history_id_filter_feedback_and_public_isolation(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    first = _history(public_harness, "1", "rdms:1001")
    cached = _history(public_harness, "2", "rdms:1001", cache_hit=True)
    _history(public_harness, "3", "rdms:1002")
    _history(public_harness, "4", "rdms:1002")
    anonymous = _history(
        public_harness, "5", "wanshitong-public:old-session"
    )
    api_call = _history(public_harness, "6", "tok_legacy")
    scope = _scope(public_harness)
    runtime.feedback.upsert(
        cached,
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        actor_owner_id="rdms:1001",
        actor_is_admin=False,
        useful=False,
        reason_code="INCOMPLETE",
    )

    base = ADMIN_BASE_PATH + "/history"
    listed = public_harness.client.get(base, params={"page_size": 20})
    assert listed.status_code == 200, listed.text
    items = {item["trace_id"]: item for item in listed.json()["items"]}
    assert listed.json()["total"] == 6
    assert items[first]["requester"] == {
        "identity_source": "RDMS_SSO",
        "external_user_id": "1001",
        "display_name_at_request": None,
        "label": "RDMS用户 #1001",
        "name_state": "NOT_CAPTURED",
    }
    assert items[cached]["cache_hit"] is True
    assert items[cached]["feedback_useful"] is False
    assert items[cached]["feedback_reason_code"] == "INCOMPLETE"
    assert len(items[cached]["answer"]) > len(items[cached]["answer_summary"])
    assert items[anonymous]["requester"]["label"] == "历史匿名会话"
    assert items[api_call]["requester"]["label"] == "API调用"

    filtered = public_harness.client.get(
        base, params={"requester_user_id": "1001", "page_size": 1}
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["items"][0]["requester"][
        "external_user_id"
    ] == "1001"
    next_page = public_harness.client.get(
        base,
        params={"requester_user_id": "1001", "page_size": 1, "offset": 1},
    )
    assert next_page.json()["total"] == 2
    assert {
        filtered.json()["items"][0]["trace_id"],
        next_page.json()["items"][0]["trace_id"],
    } == {first, cached}
    keyword_page = public_harness.client.get(
        base,
        params={
            "requester_user_id": "1001",
            "keyword": "问题 2",
            "status": "ANSWERED",
        },
    )
    assert keyword_page.status_code == 200
    assert keyword_page.json()["total"] == 1
    assert keyword_page.json()["items"][0]["trace_id"] == cached
    assert len(keyword_page.json()["items"][0]["answer"]) > 240

    generic = public_harness.client.get("/api/v1/history")
    assert generic.status_code == 200
    assert all("requester" not in item for item in generic.json()["items"])
    assert all("owner_id" not in item for item in generic.json()["items"])
    forged = public_harness.client.post(
        PUBLIC_CHAT_PATH,
        headers=public_harness.headers,
        json={
            "query": "伪造身份测试",
            "user_id": "1002",
            "display_name": "伪造姓名",
        },
    )
    assert forged.status_code == 422


def test_trace_alias_conflict_missing_and_safe_export(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    scope = _scope(public_harness)
    alias = _trace(public_harness, "a")
    missing = _trace(public_harness, "b")
    conflict = _trace(public_harness, "c")
    runtime.history.start(
        alias.removeprefix("trace_"),
        scope,
        "旧格式用户问题",
        owner_id="rdms:2001",
        save_body=False,
    )
    for trace_id, owner in (
        (conflict, "rdms:2001"),
        (conflict.removeprefix("trace_"), "rdms:2002"),
    ):
        runtime.history.start(
            trace_id,
            scope,
            "身份冲突测试",
            owner_id=owner,
            save_body=False,
        )

    base = ADMIN_BASE_PATH + "/operational-traces"
    page = public_harness.client.get(base)
    assert page.status_code == 200, page.text
    items = {item["trace_id"]: item for item in page.json()["items"]}
    assert items[alias]["requester"]["external_user_id"] == "2001"
    assert items[missing]["requester"]["name_state"] == "UNAVAILABLE"
    assert items[conflict]["requester"]["label"] == "身份关联冲突"
    assert items[conflict]["requester"]["external_user_id"] is None

    detail = public_harness.client.get(
        base + "/" + alias.removeprefix("trace_")
    )
    assert detail.status_code == 200
    assert detail.json()["trace"]["requester"]["label"] == (
        "RDMS用户 #2001"
    )
    conflicted_history = public_harness.client.get(
        ADMIN_BASE_PATH + "/history/" + conflict
    )
    assert conflicted_history.json()["requester"]["name_state"] == (
        "CONFLICT"
    )
    assert runtime.history.owner_ids_for_traces(
        [alias.removeprefix("trace_")],
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
    )[normalize_trace_id(alias)] == frozenset({"rdms:2001"})

    exported = public_harness.client.get(base + "/" + alias + "/export")
    assert exported.status_code == 200
    payload = json.loads(exported.content)
    assert "requester" not in payload["trace"]
    assert "rdms:2001" not in exported.text
