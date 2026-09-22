"""V14 专用回放脚本只接受唯一候选身份并输出安全来源诊断。"""

from __future__ import annotations

import pytest

from scripts import wb08r03_v14_live_replay as replay


def test_candidate_identity_can_advance_without_reusing_c1_to_c3() -> None:
    assert replay._validated_candidate_id("c4") == "c4"
    assert replay._validated_candidate_id("c12") == "c12"
    for invalid in ("c0", "c01", "C4", "candidate-4", ""):
        with pytest.raises(ValueError, match="CANDIDATE_ID_INVALID"):
            replay._validated_candidate_id(invalid)


def test_safe_trace_keeps_pre_analysis_source_identity_without_body() -> None:
    events = [
        (
            "retrieval.source_context",
            {
                "resolution_stage": "PRE_ANALYSIS",
                "resolution": "RESOLVED",
                "source_scope_digest": "sha256:test",
                "business_query_sha256": "business",
                "private_body": "不得进入 SAFE 摘要",
            },
        )
    ]

    safe = replay._safe_events(events)

    assert safe["retrieval.source_context"] == [
        {
            "resolution_stage": "PRE_ANALYSIS",
            "resolution": "RESOLVED",
            "source_scope_digest": "sha256:test",
            "business_query_sha256": "business",
        }
    ]
