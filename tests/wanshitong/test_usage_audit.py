"""F04 请求来源、History 修正与公共信任边界回归。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.public_api import PUBLIC_CHAT_PATH
from tests.wanshitong.support import PublicHarness, synthetic_answer


def _scope(harness: PublicHarness) -> KnowledgeBaseScope:
    binding = harness.scope_service.binding()
    return KnowledgeBaseScope(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
    )


def _audit(
    harness: PublicHarness, trace_id: str, owner_id: str
) -> QueryAuditContext:
    scope = _scope(harness)
    return QueryAuditContext(
        trace_id=trace_id,
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        owner_id=owner_id,
        deployment_id="candidate_8289",
        identity_source="RDMS_SSO",
        traffic_class="INTERACTIVE",
        classification_source="PUBLIC_ENDPOINT",
        entrypoint="manual",
        entrypoint_source="CLIENT_HINT",
    )


@pytest.mark.parametrize(
    ("client_context", "entrypoint", "source", "diagnostic"),
    [
        ({"entrypoint": "manual"}, "manual", "CLIENT_HINT", None),
        (
            {"entrypoint": "suggestion", "recommendation_id": "sq-rd-hours-01"},
            "suggestion",
            "CLIENT_HINT",
            None,
        ),
        (
            {"entrypoint": "popular", "recommendation_id": "popular-01"},
            "popular",
            "CLIENT_HINT",
            None,
        ),
        ({"entrypoint": "retry"}, "retry", "CLIENT_HINT", None),
        (None, "unknown", "NO_HINT", None),
        (
            {"entrypoint": "EVALUATION"},
            "unknown",
            "INVALID_HINT",
            "INVALID_ENTRYPOINT",
        ),
        (
            {"entrypoint": "manual", "traffic_class": "EVALUATION"},
            "unknown",
            "INVALID_HINT",
            "UNEXPECTED_FIELD",
        ),
    ],
)
def test_public_hint_is_only_advisory(
    public_harness: PublicHarness,
    client_context: object | None,
    entrypoint: str,
    source: str,
    diagnostic: str | None,
) -> None:
    body: dict[str, object] = {"query": "审计提示测试"}
    if client_context is not None:
        body["client_context"] = client_context
    response = public_harness.client.post(
        PUBLIC_CHAT_PATH, headers=public_harness.headers, json=body
    )
    assert response.status_code == 200, response.text
    trace_id = response.headers["X-Trace-Id"]
    history = public_harness.product.runtime.history.detail(
        trace_id,
        project_id=_scope(public_harness).project_id,
        knowledge_base_id=_scope(public_harness).knowledge_base_id,
    )
    usage = history["usage_audit"]
    assert usage["traffic_class"] == "INTERACTIVE"
    assert usage["effective_traffic_class"] == "INTERACTIVE"
    assert usage["identity_source"] == "ANONYMOUS_SESSION"
    assert usage["entrypoint"] == entrypoint
    assert usage["entrypoint_source"] == source
    assert usage.get("hint_diagnostic") == diagnostic
    assert usage["has_context"] is False
    assert history["question"] == "审计提示测试"


def test_public_authority_fields_are_rejected(
    public_harness: PublicHarness,
) -> None:
    for field in ("owner_id", "traffic_class", "is_admin", "run_id"):
        response = public_harness.client.post(
            PUBLIC_CHAT_PATH,
            headers=public_harness.headers,
            json={"query": "伪造权威字段", field: "EVALUATION"},
        )
        assert response.status_code == 422


def test_override_is_atomic_and_preserved_by_finish(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    scope = _scope(public_harness)
    first = "trace_" + "a" * 32
    second = "trace_" + "b" * 32
    runtime.history.start(
        first,
        scope,
        "第一个原问题",
        owner_id="rdms:1001",
        save_body=True,
        audit_context=_audit(public_harness, first, "rdms:1001"),
    )
    runtime.history.start(
        second, scope, "旧记录原问题", owner_id="rdms:1002", save_body=True
    )
    first_before = runtime.history.detail(first, project_id=scope.project_id)
    second_before = runtime.history.detail(second, project_id=scope.project_id)
    assert second_before["usage_audit"]["traffic_class"] == "LEGACY_UNKNOWN"
    endpoint = ADMIN_BASE_PATH + "/query-traffic"
    request = {
        "items": [
            {
                "trace_id": first,
                "expected_metadata_revision": first_before["metadata_revision"],
            }
        ],
        "traffic_class": "EVALUATION",
        "reason": "浏览器回放清单",
    }
    missing_csrf = public_harness.client.patch(endpoint, json=request)
    assert missing_csrf.status_code == 403
    updated = public_harness.client.patch(
        endpoint, headers=public_harness.product.write_headers, json=request
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["updated_count"] == 1
    runtime.history.finish(
        first, result=synthetic_answer(first), error=None, cancelled=False
    )
    first_after = runtime.history.detail(first, project_id=scope.project_id)
    assert first_after["usage_audit"]["traffic_class"] == "INTERACTIVE"
    assert first_after["usage_audit"]["effective_traffic_class"] == "EVALUATION"
    assert first_after["usage_audit"]["override"]["reason"] == (
        "浏览器回放清单"
    )
    assert first_after["question"] == "第一个原问题"
    assert first_after["answer"] == synthetic_answer(first).answer

    stale_batch = public_harness.client.patch(
        endpoint,
        headers=public_harness.product.write_headers,
        json={
            "items": [
                request["items"][0],
                {
                    "trace_id": second,
                    "expected_metadata_revision": second_before[
                        "metadata_revision"
                    ],
                },
            ],
            "traffic_class": "SYSTEM",
            "reason": "批量预检冲突",
        },
    )
    assert stale_batch.status_code == 409
    assert (
        runtime.history.detail(second)["usage_audit"]["effective_traffic_class"]
        == "LEGACY_UNKNOWN"
    )

    corrected = public_harness.client.patch(
        endpoint,
        headers=public_harness.product.write_headers,
        json={
            "items": [
                {
                    "trace_id": second,
                    "expected_metadata_revision": second_before[
                        "metadata_revision"
                    ],
                }
            ],
            "traffic_class": "EVALUATION",
            "reason": "登记旧浏览器回放",
        },
    )
    assert corrected.status_code == 200, corrected.text
    old_after = runtime.history.detail(second)
    assert old_after["usage_audit"]["traffic_class"] == "LEGACY_UNKNOWN"
    assert old_after["usage_audit"]["effective_traffic_class"] == ("EVALUATION")
    assert old_after["question"] == "旧记录原问题"


def test_concurrent_request_audit_is_scoped_and_does_not_change_query(
    public_harness: PublicHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = public_harness.product.runtime
    scope = _scope(public_harness)
    queries: list[dict[str, object]] = []

    def _execute(request: object, **options: object) -> object:
        del options
        assert hasattr(request, "model_dump")
        query = request.model_dump(mode="json")
        queries.append(query)
        return synthetic_answer(str(query["trace_id"]), include_evidence=False)

    monkeypatch.setattr(runtime.sdk, "_execute_search", _execute)

    def _ask(suffix: str, owner: str) -> str:
        trace_id = "trace_" + suffix * 32
        runtime.sdk.search(
            scope.project_id,
            scope.knowledge_base_id,
            "固定问题",
            owner_id=owner,
            trace_id=trace_id,
            audit_context=_audit(public_harness, trace_id, owner),
        )
        return trace_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_ask, "c", "rdms:1001")
        second = executor.submit(_ask, "d", "rdms:1002")
        trace_ids = (first.result(), second.result())
    assert len(queries) == 2
    assert all("audit_context" not in query for query in queries)
    for trace_id, owner in zip(
        trace_ids, ("rdms:1001", "rdms:1002"), strict=True
    ):
        history = runtime.history.detail(trace_id, owner_id=owner)
        assert history["usage_audit"]["traffic_class"] == "INTERACTIVE"
        assert history["usage_audit"]["identity_source"] == "RDMS_SSO"
    rows = runtime.history.list_history(
        project_id=scope.project_id, knowledge_base_id=scope.knowledge_base_id
    )
    assert rows["total"] == 2


def test_audit_does_not_change_search_request_or_add_model_calls(
    public_harness: PublicHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = public_harness.product.runtime
    scope = _scope(public_harness)
    queries: list[dict[str, object]] = []

    def _execute(request: object, **options: object) -> object:
        del options
        assert hasattr(request, "model_dump")
        query = request.model_dump(mode="json")
        queries.append(query)
        return synthetic_answer(str(query["trace_id"]), include_evidence=False)

    monkeypatch.setattr(runtime.sdk, "_execute_search", _execute)
    trace_without = "trace_" + "e" * 32
    trace_with = "trace_" + "f" * 32
    for trace_id, audit_context in (
        (trace_without, None),
        (trace_with, _audit(public_harness, trace_with, "rdms:1001")),
    ):
        runtime.sdk.search(
            scope.project_id,
            scope.knowledge_base_id,
            "完全相同的问题",
            owner_id="rdms:1001",
            trace_id=trace_id,
            audit_context=audit_context,
        )
    assert len(queries) == 2
    for query in queries:
        query.pop("trace_id")
    assert queries[0] == queries[1]
    assert (
        runtime.history.detail(trace_without)["usage_audit"]["traffic_class"]
        == "LEGACY_UNKNOWN"
    )
    assert (
        runtime.history.detail(trace_with)["usage_audit"]["traffic_class"]
        == "INTERACTIVE"
    )
