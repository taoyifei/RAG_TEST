"""旧历史只读身份边界和正文过期语义。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.product.crypto import SecretAad, SecretCipher, load_master_key
from wanshitong_gateway.legacy import LegacyHistoryReader


def test_legacy_history_is_owner_scoped_and_read_only(tmp_path: Path) -> None:
    key = tmp_path / "legacy_master_key"
    key.write_bytes(b"l" * 32)
    key.chmod(0o600)
    database = tmp_path / "legacy.sqlite3"
    trace_id = "trace_legacy_owner_1"
    cipher = SecretCipher(load_master_key(key))
    ciphertext, nonce = cipher.encrypt(
        json.dumps({"question": "旧问题", "answer": "旧答案"}),
        aad=SecretAad(
            credential_id=trace_id,
            provider_type="local-query-history",
            field_name="history-payload",
            key_version=1,
        ),
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE query_history (trace_id TEXT, owner_id TEXT, "
            "created_at TEXT, expires_at TEXT, status TEXT, "
            "body_saved INTEGER, "
            "duration_ms INTEGER, ciphertext TEXT, nonce TEXT);"
            "CREATE TABLE query_trace_events (sequence INTEGER, trace_id TEXT, "
            "occurred_at TEXT, event_name TEXT);"
        )
        connection.execute(
            "INSERT INTO query_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace_id,
                "rdms:owner-1",
                "2026-09-20T00:00:00+00:00",
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "COMPLETE",
                1,
                1000,
                ciphertext,
                nonce,
            ),
        )
        connection.execute(
            "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
            (1, trace_id, "2026-09-20T00:00:01+00:00", "answer.complete"),
        )
    reader = LegacyHistoryReader(database, key)
    assert reader.list_for_user("other") == []
    assert reader.get_for_user("other", trace_id) is None
    detail = reader.get_for_user("owner-1", trace_id)
    assert detail is not None
    assert detail["engine"] == "legacy"
    assert detail["read_only"] is True
    assert detail["question"] == "旧问题"
    assert detail["answer"] == "旧答案"
    assert detail["events"] == [
        {
            "occurred_at": "2026-09-20T00:00:01+00:00",
            "event_name": "answer.complete",
        }
    ]
