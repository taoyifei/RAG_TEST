"""端到端负载验收的固定边界。"""

from pathlib import Path

import pytest

from scripts import load_test_chat
from tests.active_evidence_fixtures import (
    active_evidence_record,
    trusted_active_evidence,
)


def test_load_test_defaults_to_five_users_for_thirty_minutes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """默认负载必须覆盖五用户持续三十分钟。"""
    monkeypatch.setattr(
        "sys.argv",
        [
            "load_test_chat.py",
            "--url",
            "http://127.0.0.1:8088",
            "--token",
            "test-token",
            "--qdrant-url",
            "http://127.0.0.1:6333",
            "--qdrant-alias",
            "test-active",
            "--manifest-database",
            str(tmp_path / "test-manifest.sqlite3"),
        ],
    )

    arguments = load_test_chat._arguments()

    assert arguments.concurrency == 5
    assert arguments.duration_seconds == 1800


def test_all_refusals_of_answerable_cases_fail_quality_gate() -> None:
    results = [
        load_test_chat.RequestResult(
            elapsed_seconds=0.2,
            outcome=load_test_chat.RequestOutcome.INCORRECT_REFUSAL,
            target=True,
            multiturn=False,
        )
        for _ in range(10)
    ]

    report = load_test_chat.summarize_results(
        results,
        concurrency=5,
        duration_seconds=1800,
    )

    assert report["answered"] == 0
    assert report["incorrect_refusals"] == 10
    assert report["passed"] is False


def test_invalid_citation_is_not_counted_as_answered() -> None:
    manifest = trusted_active_evidence(
        (
            active_evidence_record(
                chunk_id="active-chunk",
                source_path="public.docx",
                locator="第一章 > 段落1",
                text="可验证原文",
            ),
        ),
    )
    final = {
        "type": "final",
        "status": "answered",
        "claims": [
            {
                "text": "回答",
                "supports": [
                    {
                        "evidence_id": "E1",
                        "chunk_id": "forged-chunk",
                        "locator": "第一章 > 段落1",
                        "quote": "可验证原文",
                    }
                ],
            }
        ],
    }

    outcome = load_test_chat.classify_final(
        final,
        expected_answerable=True,
        active_evidence_manifest=manifest,
    )

    assert outcome == load_test_chat.RequestOutcome.INVALID_CITATION
