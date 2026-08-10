from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from deployment.industry import serving_runtime_check

_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_FINGERPRINT = (
    "sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a"
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.status = 200
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _manifest_database(path: Path) -> None:
    manifest = json.dumps(
        {
            "collection_name": "rag-docx-active",
            "pipeline_fingerprint": _FINGERPRINT,
            "sources": [{"source_id": "source-1"}],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE index_manifests ("
            "collection_name TEXT, pipeline_fingerprint TEXT, "
            "manifest_json TEXT, manifest_sha256 TEXT, state TEXT)"
        )
        connection.execute(
            "INSERT INTO index_manifests VALUES (?, ?, ?, ?, 'active')",
            (
                "rag-docx-active",
                _FINGERPRINT,
                manifest,
                hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
            ),
        )


def test_pre_update_helper_reads_old_index_without_mutating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "manifest.sqlite3"
    _manifest_database(database)
    before = (database.read_bytes(), database.stat().st_mode)
    monkeypatch.setenv("RAG_MANIFEST_DATABASE", str(database))
    monkeypatch.setenv("RAG_QDRANT_ALIAS", "rag-industry-active")
    monkeypatch.setenv("RAG_RELEASE_REVISION", _REVISION)
    monkeypatch.setenv("RAG_QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("RAG_QDRANT_API_KEY", "private-api-key")

    def urlopen(request: object, *, timeout: int) -> _Response:
        assert timeout == 30
        assert request.headers["Api-key"] == "private-api-key"
        if str(request.full_url).endswith("/aliases"):
            return _Response(
                {
                    "result": {
                        "aliases": [
                            {
                                "alias_name": "rag-industry-active",
                                "collection_name": "rag-docx-active",
                            }
                        ]
                    }
                }
            )
        return _Response({"result": {"count": 139}})

    monkeypatch.setattr(
        serving_runtime_check.urllib.request, "urlopen", urlopen
    )

    result = serving_runtime_check.pre_update_index_state()

    assert result == {
        "active_collection": "rag-docx-active",
        "alias": "rag-industry-active",
        "index_fingerprint": _FINGERPRINT,
        "manifest_sha256": result["manifest_sha256"],
        "payload_schema": "industry-pre-update-index-state-v1",
        "point_count": 139,
        "release_revision": _REVISION,
        "source_count": 1,
    }
    assert len(str(result["manifest_sha256"])) == 64
    assert (database.read_bytes(), database.stat().st_mode) == before
    assert "private-api-key" not in json.dumps(result)


def test_trace_backup_records_complete_identity_and_uses_private_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "traces.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO traces VALUES ('trace-1')")
        connection.execute("PRAGMA user_version=1")
    source.chmod(0o600)
    source_bytes = source.read_bytes()
    destination = tmp_path / "backup" / "traces-before.sqlite3"

    report = serving_runtime_check.backup_trace_database(
        source,
        destination,
        _REVISION,
    )

    assert report["target_revision"] == _REVISION
    assert report["sqlite_user_version"] == 1
    assert isinstance(report["page_count"], int)
    assert report["page_count"] > 0
    assert report["mode"] == "0600"
    assert report["source_filename"] == "traces.sqlite3"
    assert report["source_database_identity"]["bytes"] == len(source_bytes)  # type: ignore[index]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert source.read_bytes() == source_bytes
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == (
            "ok",
        )
        assert connection.execute("SELECT trace_id FROM traces").fetchone() == (
            "trace-1",
        )


def test_trace_backup_rejects_bad_revision_and_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "traces.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    source.chmod(0o600)
    destination = tmp_path / "backup.sqlite3"

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="TARGET_REVISION",
    ):
        serving_runtime_check.backup_trace_database(source, destination, "bad")
    destination.touch()
    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="DESTINATION_EXISTS",
    ):
        serving_runtime_check.backup_trace_database(
            source, destination, _REVISION
        )
