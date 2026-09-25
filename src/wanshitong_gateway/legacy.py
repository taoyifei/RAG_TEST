"""只读旧版问答快照；旧引擎不参与任何新请求。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rag_app.product.crypto import SecretAad, SecretCipher, load_master_key

_MAX_LIST = 100


class LegacyHistoryReader:
    """从切换前的一致性备份读取本人的旧记录。"""

    def __init__(self, database: Path, master_key_file: Path) -> None:
        if database.is_symlink() or not database.is_file():
            raise ValueError("旧历史必须来自现有非 symlink 备份。")
        self._database = database
        self._cipher = SecretCipher(load_master_key(master_key_file))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self._database}?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        return connection

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """仅列快照中属于当前 RDMS 用户的最近记录。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trace_id, created_at, expires_at, status, "
                "body_saved, duration_ms FROM query_history "
                "WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                (f"rdms:{user_id}", _MAX_LIST),
            ).fetchall()
        return [self._summary(row) for row in rows]

    def get_for_user(
        self, user_id: str, trace_id: str
    ) -> dict[str, Any] | None:
        """按旧 Trace ID 取本人记录及最低限度旧事件。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT trace_id, created_at, expires_at, status, body_saved, "
                "duration_ms, ciphertext, nonce FROM query_history "
                "WHERE owner_id=? AND trace_id=?",
                (f"rdms:{user_id}", trace_id),
            ).fetchone()
            if row is None:
                return None
            events = connection.execute(
                "SELECT occurred_at, event_name FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence LIMIT 1000",
                (trace_id,),
            ).fetchall()
        result = self._summary(row)
        result["events"] = [
            {
                "occurred_at": item["occurred_at"],
                "event_name": item["event_name"],
            }
            for item in events
        ]
        result["question"] = None
        result["answer"] = None
        if (
            result["body_available"]
            and isinstance(row["ciphertext"], str)
            and isinstance(row["nonce"], str)
        ):
            decoded = json.loads(
                self._cipher.decrypt(
                    row["ciphertext"],
                    row["nonce"],
                    aad=SecretAad(
                        credential_id=trace_id,
                        provider_type="local-query-history",
                        field_name="history-payload",
                        key_version=1,
                    ),
                )
            )
            if isinstance(decoded, dict):
                for field in ("question", "answer"):
                    value = decoded.get(field)
                    result[field] = value if isinstance(value, str) else None
        return result

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        expired = expires_at <= datetime.now(UTC)
        return {
            "engine": "legacy",
            "read_only": True,
            "trace_id": str(row["trace_id"]),
            "created_at": str(row["created_at"]),
            "status": str(row["status"]),
            "duration_ms": row["duration_ms"],
            "body_available": bool(row["body_saved"]) and not expired,
            "body_unavailable_reason": "EXPIRED" if expired else (
                "NOT_SAVED" if not row["body_saved"] else None
            ),
        }
