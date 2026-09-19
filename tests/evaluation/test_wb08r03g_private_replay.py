"""WB08R-03G 私有重放的只读 Schema 与脱敏合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.wb08r03g_private_replay import (
    inspect_trace_schema,
    read_history_identity,
    read_trace_events,
    safe_observation,
)


def _trace_database(path: Path) -> None:
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE query_history ("
            "trace_id TEXT PRIMARY KEY, question_sha256 TEXT, status TEXT)"
        )
        database.execute(
            "CREATE TABLE query_trace_events ("
            "trace_id TEXT, sequence INTEGER, event_name TEXT, "
            "payload_json TEXT)"
        )
        database.execute(
            "INSERT INTO query_history VALUES (?, ?, ?)",
            ("trace_1", "question-hash", "COMPLETED"),
        )
        database.execute(
            "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
            (
                "trace_1",
                1,
                "retrieval.atom_grounding",
                '{"claim_rejection_diagnostics":[{'
                '"raw_reason_code":"CLAIM_SUPPORT_OUTSIDE_ATOM",'
                '"claim_sha256":"claim-hash"}],'
                '"private_text":"不得进入 SAFE manifest"}',
            ),
        )


def test_private_replay_inspects_schema_before_fixed_read(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trace.sqlite3"
    _trace_database(database_path)

    schema = inspect_trace_schema(database_path)
    events = read_trace_events(database_path, "trace_1")
    history = read_history_identity(database_path, "trace_1")

    assert set(schema) == {"query_history", "query_trace_events"}
    assert events[0][0] == "retrieval.atom_grounding"
    assert history == {
        "trace_id": "trace_1",
        "question_sha256": "question-hash",
        "status": "COMPLETED",
    }


def test_private_replay_rejects_wrong_trace_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "trace.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(ValueError, match="TRACE_SCHEMA_TABLE_MISMATCH"):
        inspect_trace_schema(database_path)


def test_safe_observation_keeps_hashes_and_drops_raw_text() -> None:
    events = (
        (
            "retrieval.atom_grounding",
            {
                "claim_rejection_diagnostics": [
                    {
                        "raw_reason_code": "CLAIM_SUPPORT_OUTSIDE_ATOM",
                        "claim_sha256": "claim-hash",
                        "claim_text": "不得进入 SAFE manifest 的 Claim",
                    }
                ],
                "private_text": "不应输出的模型原文",
            },
        ),
    )
    observation = safe_observation(
        "WB08R-N-033",
        "合成问题",
        {
            "trace_id": "trace_1",
            "status": "INSUFFICIENT_EVIDENCE",
            "reason_code": "CLAIM_NOT_SUPPORTED",
            "answer": "合成回答",
            "citations": [{"quote": "合成引文"}],
        },
        events,
        None,
    )

    serialized = str(observation)
    assert "合成问题" not in serialized
    assert "合成回答" not in serialized
    assert "合成引文" not in serialized
    assert "不应输出的模型原文" not in serialized
    assert "不得进入 SAFE manifest 的 Claim" not in serialized
    assert "CLAIM_SUPPORT_OUTSIDE_ATOM" in serialized
    assert observation["history_identity"] == "NOT_OBSERVED"
