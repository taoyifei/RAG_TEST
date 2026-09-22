"""阶段 05 反馈原子写入与兼容合同回归。"""

from __future__ import annotations

from typing import NoReturn

import pytest

from rag_app.core.errors import RagError
from rag_app.core.models import KnowledgeBaseScope
from rag_app.tracing.store import TraceNotFoundError
from rag_app.wanshitong.public_api import PUBLIC_FEEDBACK_PATH
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
        "反馈闭环测试问题",
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
