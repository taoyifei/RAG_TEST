"""V8 评测不能混淆候选包、实际发送、直接摘录或缺失观测。"""

from __future__ import annotations

from evaluation.wanshitong.v2.wb08r03g_v8_metrics import (
    first_loss,
    packet_observation,
    performance_observation,
)


def test_old_trace_without_sent_packet_remains_not_observed() -> None:
    assert packet_observation({})["prepared_sent_status"] == "NOT_OBSERVED"


def test_server_direct_path_does_not_fabricate_a_sent_packet() -> None:
    observed = packet_observation(
        {
            "retrieval.claim_publication": [
                {"generation_called": False, "published_claim_count": 1}
            ]
        }
    )
    assert observed["prepared_sent_status"] == "N/A_SERVER_DIRECT"
    assert observed["attempts"] == []


def test_attempt_aliases_can_repeat_without_sharing_identity() -> None:
    packets = [
        {
            "request_id": "request-one",
            "attempt_id": attempt,
            "alias_to_support_key": [["S1", key]],
            "per_atom_support_ids": [["A1", ["S1"]]],
            "messages_sha256": "sha256:" + "b" * 64,
            "support_sources": [{"support_key": key}],
            "evidence_level": "TRANSPORT_SENT",
            "transport_body_sha256": "sha256:" + "a" * 64,
        }
        for attempt, key in (("initial", "source-a"), ("repair", "source-b"))
    ]
    observed = packet_observation(
        {
            "retrieval.claim_publication": [
                {
                    "prepared_packets": packets,
                    "publication_path": "MODEL_VALIDATED_CLAIM",
                    "published_support_keys": ["source-a", "source-b"],
                }
            ]
        }
    )
    assert observed["prepared_sent_status"] == "OBSERVED"
    assert observed["fact_coverage"] == "NOT_CERTIFIED"
    assert observed["cache_hit"] == "NOT_OBSERVED"


def test_truncated_stage_cannot_be_reported_as_gold_missing() -> None:
    result = first_loss(
        [{"stage": "raw", "candidates": [], "truncated": True}],
        document_version_id="version-a",
        chunk_id="chunk-a",
    )
    assert result["reason"] == "NOT_OBSERVED_COMPLETE_ID_SET"


def test_first_loss_reports_the_first_complete_stage() -> None:
    candidate = {"document_version_id": "version-a", "chunk_id": "chunk-a"}
    result = first_loss(
        [
            {
                "stage": "raw",
                "candidates": [candidate],
                "total": 1,
                "truncated": False,
            },
            {
                "stage": "fusion",
                "candidates": [],
                "total": 0,
                "truncated": False,
                "drop_reason": "QUOTA_LIMIT",
            },
        ],
        document_version_id="version-a",
        chunk_id="chunk-a",
    )
    assert result == {"stage": "fusion", "reason": "QUOTA_LIMIT"}


def test_missing_total_cannot_certify_a_complete_stage() -> None:
    result = first_loss(
        [{"stage": "raw", "candidates": [], "truncated": False}],
        document_version_id="version-a",
        chunk_id="chunk-a",
    )
    assert result["reason"] == "NOT_OBSERVED_COMPLETE_ID_SET"


def test_malformed_alias_observation_fails_without_crashing() -> None:
    observed = packet_observation(
        {
            "retrieval.claim_publication": [
                {"prepared_packets": [{"alias_to_support_key": [["S1"]]}]}
            ]
        }
    )
    assert observed["prepared_sent_status"] == "FAILED"
    assert "INVALID_ALIAS_MAP" in observed["packet_failures"]


def test_refusal_latency_cannot_substitute_for_generation_or_ttft() -> None:
    result = performance_observation(
        [
            {
                "status": "INSUFFICIENT_EVIDENCE",
                "request_total_ms": 100,
                "stage_status_first_ms": 10,
            },
            {
                "answer_path": "LLM_CLAIM_VALIDATED",
                "request_total_ms": 20000,
                "stage_status_first_ms": 20,
                "first_answer_event_ms": 18000,
            },
        ]
    )
    assert result["refuse"]["end_to_end"]["p95_ms"] == 100
    assert result["generate"]["end_to_end"]["p95_ms"] == 20000
    assert result["generate"]["model_ttft"] == "NOT_OBSERVED"
    assert result["direct"]["end_to_end"]["sample_count"] == 0
    assert result["direct"]["end_to_end"]["p95_ms"] == "NOT_OBSERVED"


def test_model_publication_requires_actual_sent_support_keys() -> None:
    observed = packet_observation(
        {
            "retrieval.claim_publication": [
                {
                    "publication_path": "MODEL_VALIDATED_CLAIM",
                    "published_support_keys": ["not-sent"],
                    "prepared_packets": [
                        {
                            "request_id": "r",
                            "attempt_id": "initial",
                            "alias_to_support_key": [["S1", "sent"]],
                            "per_atom_support_ids": [["A1", ["S1"]]],
                            "support_sources": [{"support_key": "sent"}],
                            "messages_sha256": "sha256:" + "b" * 64,
                            "transport_body_sha256": "sha256:" + "a" * 64,
                            "evidence_level": "TRANSPORT_SENT",
                        }
                    ],
                }
            ]
        }
    )
    assert observed["prepared_sent_status"] == "FAILED"
    assert "PUBLISHED_MODEL_SUPPORT_NOT_SENT" in observed["packet_failures"]
