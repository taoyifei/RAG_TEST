"""阶段 05 反馈原子写入与兼容合同回归。"""

from __future__ import annotations

from typing import NoReturn

import pytest

from rag_app.core.errors import RagError
from rag_app.core.models import KnowledgeBaseScope
from rag_app.tracing.store import TraceNotFoundError
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.feedback import project_feedback_trace
from rag_app.wanshitong.public_api import PUBLIC_FEEDBACK_PATH
from tests.api.test_query_history import _upload
from tests.wanshitong.support import (
    PublicHarness,
    public_principal,
    synthetic_answer,
)


def _finish_history(
    harness: PublicHarness,
    trace_id: str,
    *,
    failed: bool = False,
    question: str = "反馈闭环测试问题",
) -> None:
    binding = harness.scope_service.binding()
    owner_id = public_principal(
        harness, harness.client, harness.csrf
    ).owner_id
    harness.product.runtime.history.start(
        trace_id,
        KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        ),
        question,
        owner_id=owner_id,
        save_body=True,
    )
    harness.product.runtime.history.finish(
        trace_id,
        result=None if failed else synthetic_answer(trace_id),
        error=(
            RagError("合成失败。", stage="test", code="SYNTHETIC_FAILURE")
            if failed
            else None
        ),
        cancelled=False,
    )


def test_trace_projection_separates_provider_and_app_status() -> None:
    projection = project_feedback_trace(
        {
            "status": "ANSWERED",
            "result": {
                "status": "ANSWERABLE",
                "reason_code": "ANSWER_SUPPORTED",
            },
        },
        {
            "spans": [
                {
                    "name": "provider.generation",
                    "status": "ERROR",
                    "reason_code": "PROVIDER_FAILED",
                }
            ]
        },
    )

    assert projection["validation_coverage"]["provider_statuses"] == {
        "generation": {
            "status": "ERROR",
            "reason_code": "PROVIDER_FAILED",
        }
    }
    assert projection["validation_coverage"][
        "application_validation_status"
    ] == {
        "status": "ANSWERABLE",
        "reason_code": "ANSWER_SUPPORTED",
    }


def test_feedback_details_are_encrypted_idempotent_and_clear_on_positive(
    public_harness: PublicHarness,
) -> None:
    trace_id = "trace_" + "8" * 32
    _finish_history(public_harness, trace_id)
    payload = {
        "trace_id": trace_id,
        "useful": False,
        "reason_code": "WRONG_SOURCE",
        "reason_detail": "WRONG_SOURCE",
        "comment": "应该引用办事指南的最新版本。",
    }

    first = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json=payload,
    )
    repeated = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json=payload,
    )

    assert first.status_code == 200, first.text
    assert first.json()["reason_detail"] == "WRONG_SOURCE"
    assert first.json()["comment_saved"] is True
    assert first.json()["feedback_revision"] == 1
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["feedback_revision"] == 1
    with public_harness.product.runtime.connections.transaction() as connection:
        canonical_count = connection.execute(
            "SELECT COUNT(*) FROM product_feedback WHERE trace_id=?",
            (trace_id,),
        ).fetchone()[0]
        detail = connection.execute(
            "SELECT * FROM wanshitong_feedback_details WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    assert canonical_count == 1
    assert detail is not None
    assert detail["comment_ciphertext"] != payload["comment"]
    assert detail["comment_nonce"]
    assert detail["encryption_key_id"].startswith("sha256:")

    positive = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={"trace_id": trace_id, "useful": True},
    )
    assert positive.status_code == 200, positive.text
    assert positive.json()["feedback_revision"] == 2
    with public_harness.product.runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT f.reason_code, d.reason_detail, d.comment_ciphertext "
            "FROM product_feedback f JOIN wanshitong_feedback_details d "
            "USING(trace_id) WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    assert tuple(row) == (None, None, None)


def test_old_negative_request_defaults_to_other_and_failed_is_feedbackable(
    public_harness: PublicHarness,
) -> None:
    trace_id = "trace_" + "9" * 32
    _finish_history(public_harness, trace_id, failed=True)

    response = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={"trace_id": trace_id, "useful": False},
    )

    assert response.status_code == 200, response.text
    assert response.json()["reason_code"] == "OTHER"
    assert response.json()["reason_detail"] == "OTHER"


def test_invalid_feedback_combinations_are_rejected_without_writes(
    public_harness: PublicHarness,
) -> None:
    trace_id = "trace_" + "6" * 32
    _finish_history(public_harness, trace_id)

    positive_with_reason = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": True,
            "reason_detail": "INCOMPLETE",
        },
    )
    mismatched_reason = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": False,
            "reason_code": "INCORRECT",
            "reason_detail": "WRONG_SOURCE",
        },
    )
    oversized_comment = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": False,
            "comment": "字" * 1001,
        },
    )

    assert positive_with_reason.status_code == 422
    assert mismatched_reason.status_code == 400
    assert oversized_comment.status_code == 422
    with public_harness.product.runtime.connections.transaction() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM product_feedback WHERE trace_id=?", (trace_id,)
            ).fetchone()
            is None
        )


def test_detail_write_failure_rolls_back_canonical_feedback(
    public_harness: PublicHarness,
) -> None:
    trace_id = "trace_" + "a" * 32
    _finish_history(public_harness, trace_id)
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "CREATE TRIGGER fail_wanshitong_feedback_detail "
            "BEFORE INSERT ON wanshitong_feedback_details "
            "BEGIN SELECT RAISE(ABORT, 'synthetic detail failure'); END"
        )

    response = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={"trace_id": trace_id, "useful": False},
    )

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "FEEDBACK_STORE_UNAVAILABLE"
    with public_harness.product.runtime.connections.transaction() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM product_feedback WHERE trace_id=?", (trace_id,)
            ).fetchone()
            is None
        )


def test_projection_failure_keeps_canonical_feedback_pending(
    public_harness: PublicHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_id = "trace_" + "b" * 32
    _finish_history(public_harness, trace_id)

    def _missing_projection(
        projected_trace_id: str, *, useful: bool
    ) -> NoReturn:
        del useful
        raise TraceNotFoundError(projected_trace_id)

    monkeypatch.setattr(
        public_harness.product.runtime.feedback,
        "_projector",
        _missing_projection,
    )
    response = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={"trace_id": trace_id, "useful": False},
    )

    assert response.status_code == 200, response.text
    assert response.json()["projection_state"] == "PENDING"
    with public_harness.product.runtime.connections.transaction() as connection:
        assert connection.execute(
            "SELECT 1 FROM product_feedback f JOIN "
            "wanshitong_feedback_details d USING(trace_id) "
            "WHERE f.trace_id=?",
            (trace_id,),
        ).fetchone()


def test_five_question_replay_is_unchanged_after_feedback(
    public_harness: PublicHarness,
) -> None:
    questions = (
        "办理事项需要哪些材料？",
        "线上办理入口在哪里？",
        "办理时限是多少？",
        "哪些情况会被拒绝？",
        "如何查询办理进度？",
    )
    snapshots: dict[str, dict[str, object]] = {}
    for marker, question in zip("def12", questions, strict=True):
        trace_id = "trace_" + marker * 32
        _finish_history(public_harness, trace_id, question=question)
        detail = public_harness.product.runtime.history.detail(trace_id)
        snapshots[trace_id] = {
            key: detail[key]
            for key in ("question", "answer", "result", "status")
        }
        response = public_harness.client.post(
            PUBLIC_FEEDBACK_PATH,
            headers=public_harness.headers,
            json={
                "trace_id": trace_id,
                "useful": marker in {"d", "1"},
                **(
                    {}
                    if marker in {"d", "1"}
                    else {"reason_detail": "INCOMPLETE"}
                ),
            },
        )
        assert response.status_code == 200, response.text

    for trace_id, snapshot in snapshots.items():
        detail = public_harness.product.runtime.history.detail(trace_id)
        assert {
            key: detail[key]
            for key in ("question", "answer", "result", "status")
        } == snapshot

    page = public_harness.client.get(
        ADMIN_BASE_PATH + "/feedback", params={"page_size": 2, "offset": 0}
    )
    filtered = public_harness.client.get(
        ADMIN_BASE_PATH + "/feedback",
        params={"reason": "INCOMPLETE", "page_size": 100},
    )
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 5
    assert len(page.json()["items"]) == 2
    assert page.json()["next_offset"] == 2
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 3


def test_legacy_canonical_feedback_without_details_remains_reviewable(
    public_harness: PublicHarness,
) -> None:
    trace_id = "trace_" + "4" * 32
    _finish_history(public_harness, trace_id)
    binding = public_harness.scope_service.binding()
    owner_id = public_principal(
        public_harness, public_harness.client, public_harness.csrf
    ).owner_id
    public_harness.product.runtime.feedback.upsert(
        trace_id,
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        actor_owner_id=owner_id,
        actor_is_admin=False,
        useful=False,
        reason_code="OUTDATED",
    )

    with public_harness.product.runtime.connections.transaction() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM wanshitong_feedback_details WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            is None
        )
    detail = public_harness.client.get(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}"
    )
    statistics = public_harness.client.get(
        ADMIN_BASE_PATH + "/feedback/statistics"
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["feedback"]["reason_code"] == "OUTDATED"
    assert detail.json()["feedback"]["reason_detail"] is None
    assert statistics.status_code == 200, statistics.text
    assert statistics.json()["evaluated_count"] == 1

    reviewed = public_harness.client.patch(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}/review",
        headers=public_harness.product.write_headers,
        json={
            "expected_version": 0,
            "review_status": "REVIEWED",
            "root_cause": "OTHER",
            "note": "旧反馈按原有原因复核。",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review"]["reviewed_feedback_revision"] == 1

    repeated = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": False,
            "reason_code": "OUTDATED",
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["reason_detail"] is None
    assert repeated.json()["feedback_revision"] == 1

    changed = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": False,
            "reason_detail": "INCOMPLETE",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["feedback_revision"] == 2
    refreshed = public_harness.client.get(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}"
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["review"]["new_feedback_pending"] is True


def test_admin_feedback_detail_review_conflict_statistics_and_safe_export(  # noqa: PLR0915
    public_harness: PublicHarness,
) -> None:
    binding = public_harness.scope_service.binding()
    document_id = _upload(
        public_harness.product,
        binding.project_id,
        binding.knowledge_base_id,
    )
    document = public_harness.product.runtime.sdk.get_document(
        binding.project_id,
        binding.knowledge_base_id,
        document_id,
    )
    assert document.current_version_id is not None
    trace_id = "trace_" + "c" * 32
    owner_id = public_principal(
        public_harness, public_harness.client, public_harness.csrf
    ).owner_id
    result = synthetic_answer(trace_id)
    evidence = result.evidence[0].model_copy(
        update={
            "document_id": document_id,
            "document_version_id": document.current_version_id,
        }
    )
    runtime = public_harness.product.runtime
    runtime.history.start(
        trace_id,
        KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        ),
        "管理员需要核对哪份办事指南？",
        owner_id=owner_id,
        save_body=True,
    )
    runtime.history.finish(
        trace_id,
        result=result.model_copy(update={"evidence": (evidence,)}),
        error=None,
        cancelled=False,
    )
    comment = "用户说明：引用的版本不对。"
    saved = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": False,
            "reason_detail": "WRONG_SOURCE",
            "comment": comment,
        },
    )
    assert saved.status_code == 200, saved.text

    listing = public_harness.client.get(ADMIN_BASE_PATH + "/feedback")
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["question_summary"].startswith(
        "管理员需要核对"
    )
    detail = public_harness.client.get(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}"
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["feedback"]["comment"] == comment
    assert detail.json()["history"]["answer"]
    assert detail.json()["trace_projection"]["question_understanding"][
        "context_mode"
    ] is None
    assert detail.json()["operational_trace"]["trace"]["status"] == (
        "NOT_CAPTURED_BEFORE_V3"
    )

    note = "人工核对后确认来源选择错误。"
    review_body = {
        "expected_version": 0,
        "review_status": "REVIEWED",
        "root_cause": "WRONG_SOURCE",
        "note": note,
        "selected_source_document_id": document_id,
        "selected_source_version_id": document.current_version_id,
        "evaluation_candidate": True,
        "verification_references": [],
    }
    reviewed = public_harness.client.patch(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}/review",
        headers=public_harness.product.write_headers,
        json=review_body,
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review"]["review_version"] == 1
    assert reviewed.json()["review"]["note"] == note
    assert reviewed.json()["review"]["new_feedback_pending"] is False

    unresolved = public_harness.client.patch(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}/review",
        headers=public_harness.product.write_headers,
        json={
            **review_body,
            "expected_version": 1,
            "review_status": "RESOLVED",
            "fix_reference": None,
            "verification_references": [],
        },
    )
    assert unresolved.status_code == 400, unresolved.text
    assert unresolved.json()["error"]["code"] == "INVALID_INPUT"

    conflict = public_harness.client.patch(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}/review",
        headers=public_harness.product.write_headers,
        json=review_body,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "CONFLICT"

    changed = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": trace_id,
            "useful": False,
            "reason_detail": "INCOMPLETE",
        },
    )
    assert changed.status_code == 200, changed.text
    changed_detail = public_harness.client.get(
        ADMIN_BASE_PATH + f"/feedback/{trace_id}"
    ).json()
    assert changed_detail["review"]["review_status"] == "REVIEWED"
    assert changed_detail["review"]["new_feedback_pending"] is True

    sso_trace_id = "trace_" + "5" * 32
    runtime.history.start(
        sso_trace_id,
        KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        ),
        "SSO 统计分组测试问题",
        owner_id="rdms:statistics-test-user",
        save_body=True,
    )
    runtime.history.finish(
        sso_trace_id,
        result=synthetic_answer(sso_trace_id),
        error=None,
        cancelled=False,
    )
    with runtime.connections.transaction(write=True) as connection:
        connection.execute(
            "UPDATE query_history SET duration_ms=? WHERE trace_id=?",
            (812, trace_id),
        )
        connection.execute(
            "UPDATE query_history SET duration_ms=? WHERE trace_id=?",
            (120, sso_trace_id),
        )

    statistics = public_harness.client.get(
        ADMIN_BASE_PATH + "/feedback/statistics"
    )
    assert statistics.status_code == 200, statistics.text
    assert statistics.json()["evaluated_count"] == 1
    assert statistics.json()["pending_count"] == 1
    assert statistics.json()["confirmed_wrong_source_count"] == 1
    assert statistics.json()["latency_ms"] == {
        "actual_sso_users": {"count": 1, "p50": 120, "p95": 120},
        "non_sso_or_replay": {"count": 1, "p50": 812, "p95": 812},
    }

    exported = public_harness.client.post(
        ADMIN_BASE_PATH + "/feedback/export",
        headers=public_harness.product.write_headers,
        json={"trace_ids": [trace_id]},
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["cache-control"] == "no-store"
    payload = exported.json()
    assert payload["content_policy"] == "metadata_only"
    assert payload["items"][0]["feedback_id"] == trace_id
    assert payload["items"][0]["selected_source"] == {
        "document_id": document_id,
        "document_version_id": document.current_version_id,
    }
    assert comment not in exported.text
    assert note not in exported.text
    assert "管理员需要核对哪份办事指南" not in exported.text
    assert "办理材料应在五个工作日" not in exported.text

    with runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT note_ciphertext FROM wanshitong_feedback_reviews "
            "WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    assert row is not None
    assert row["note_ciphertext"] != note
