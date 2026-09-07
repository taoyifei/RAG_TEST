from __future__ import annotations

from typing import NoReturn

import httpx
import pytest

from evaluation.p11_pilot_data import load_pilot_dataset
from rag_app.product.budget_plan import build_p11_budget_plan


def test_complete_plan_keeps_thirty_questions_and_never_sends_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_http(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("预算方案不能发 HTTP。")

    monkeypatch.setattr(httpx.Client, "send", no_http)
    before = load_pilot_dataset()
    history = {
        "reserved": 6,
        "estimated_input_tokens": 157,
        "observed_tokens": 242,
        "unknown_usage_attempts": 3,
        "providers": {
            "jina": {"reserved": 4, "estimated_input_tokens": 119},
            "aliyun": {"reserved": 2, "estimated_input_tokens": 38},
        },
    }
    plan = build_p11_budget_plan(history)
    after = load_pilot_dataset()
    assert before == after
    assert plan["status"] == "PROPOSED"
    assert not plan["activated"]
    assert plan["approver"] is None
    assert plan["sample_count"] == 30
    assert plan["lanes"] == ["primary", "standby"]
    assert not plan["quality_thresholds_changed"]
    assert plan["new_provider_http"] == 0
    assert plan["query_embedding_only_lower_bound"] == {
        "requests": 60,
        "estimated_input_tokens": 3590,
    }
    operations = {row["operation"] for row in plan["operations"]}
    assert {
        "pilot.embedding.document",
        "pilot.embedding.query",
        "pilot.reranking",
        "functional.primary_query.embedding.query",
        "functional.standby_failover.embedding.query",
        "functional.standby_unavailable.reranking",
        "retry.allowance",
        "canary.embedding.document",
        "canary.embedding.query",
        "canary.reranking",
    } <= operations
    totals = plan["totals_additional_work"]
    for metric in ("requests", "estimated_input_tokens"):
        assert totals["lower_bound"][metric] < totals["planned"][metric]
        assert totals["planned"][metric] <= totals["capped_max"][metric]
    assert plan["current_authorization"]["remaining_requests"] == 19
    assert plan["current_authorization"]["remaining_estimated_tokens"] == 843
    assert plan["per_provider"]["jina"]["remaining_estimated_tokens"] == 481
    assert plan["local_fault_injection"]["provider_http"] == 0
    step_limits = plan["proposed_cumulative_limits"]["step_request_limits"]
    assert step_limits["aliyun_document_canary"] == 1
    assert step_limits["aliyun_query_canary"] == 1
    assert step_limits["jina_connection"] == 3
    assert all(
        row["observed_tokens_if_known"] is None for row in plan["operations"]
    )
    assert (
        plan["proposed_cumulative_limits"]["request_limit"]
        == 6 + totals["capped_max"]["requests"]
    )
