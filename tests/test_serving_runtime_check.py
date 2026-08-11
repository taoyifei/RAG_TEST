from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from deployment.industry import serving_runtime_check

_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_FINGERPRINT = (
    "sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a"
)
_CONFIG_NAMES = {
    "corpus-policy.json",
    "intent-router-calibration.json",
    "intent-router.json",
    "pipeline.json",
    "retrieval.json",
}


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
    assert report["source_database_identity"]["file_type"] == "regular"  # type: ignore[index]
    assert report["source_database_observation"]["before"]["bytes"] == (  # type: ignore[index]
        len(source_bytes)
    )
    assert report["source_changed_during_backup"] is False
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert source.read_bytes() == source_bytes
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == (
            "ok",
        )
        assert connection.execute("SELECT trace_id FROM traces").fetchone() == (
            "trace-1",
        )


def test_pre_update_filesystem_state_is_exact_private_and_path_free(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in _CONFIG_NAMES:
        path = config / name
        path.write_text(json.dumps({"name": name}) + "\n", encoding="utf-8")
        path.chmod(0o600)
    database = tmp_path / "traces.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
    database.chmod(0o600)
    before = {
        path.name: (path.read_bytes(), path.stat()) for path in config.iterdir()
    }
    database_before = (database.read_bytes(), database.stat())

    report = serving_runtime_check.pre_update_filesystem_state(
        config, database, "first-deploy-private-v1"
    )

    assert set(report["config"]["files"]) == _CONFIG_NAMES  # type: ignore[index]
    assert report["config"]["profile"] == "first-deploy-private-v1"  # type: ignore[index]
    for identity in report["config"]["files"].values():  # type: ignore[union-attr]
        assert set(identity) == {"gid", "mode", "sha256", "uid"}
        assert identity["mode"] == "0600"
    assert report["trace"] == {
        "filename": "traces.sqlite3",
        "mode": "0600",
        "sqlite_user_version": 1,
    }
    serialized = json.dumps(report, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "question_text" not in serialized
    assert {
        path.name: (path.read_bytes(), path.stat()) for path in config.iterdir()
    } == before
    assert (database.read_bytes(), database.stat()) == database_before


def test_pre_update_filesystem_state_rejects_extra_and_public_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in _CONFIG_NAMES:
        path = config / name
        path.write_text("{}\n", encoding="utf-8")
        path.chmod(0o600)
    database = tmp_path / "traces.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    database.chmod(0o600)
    extra = config / "extra.json"
    extra.write_text("{}\n", encoding="utf-8")
    extra.chmod(0o600)

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="CONFIG_EXACT_SET",
    ):
        serving_runtime_check.pre_update_filesystem_state(
            config, database, "first-deploy-private-v1"
        )
    extra.unlink()
    (config / "pipeline.json").chmod(0o644)
    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="CONFIG_FILE_MODE",
    ):
        serving_runtime_check.pre_update_filesystem_state(
            config, database, "first-deploy-private-v1"
        )


@pytest.mark.parametrize(
    ("profile", "mode"),
    (
        ("first-deploy-private-v1", 0o600),
        ("serving-runtime-public-config-v1", 0o644),
    ),
)
def test_config_profiles_accept_only_their_exact_read_only_mode(
    tmp_path: Path,
    profile: str,
    mode: int,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in _CONFIG_NAMES:
        path = config / name
        path.write_text("{}\n", encoding="utf-8")
        path.chmod(mode)
    database = tmp_path / "traces.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    database.chmod(0o600)

    report = serving_runtime_check.pre_update_filesystem_state(
        config, database, profile
    )

    assert report["config"]["profile"] == profile  # type: ignore[index]
    assert {
        item["mode"]
        for item in report["config"]["files"].values()  # type: ignore[union-attr]
    } == {f"{mode:04o}"}

    (config / "pipeline.json").chmod(0o664)
    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="CONFIG_FILE_MODE",
    ):
        serving_runtime_check.pre_update_filesystem_state(
            config, database, profile
        )


def test_config_profile_rejects_sha_drift_and_symlink(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in _CONFIG_NAMES:
        path = config / name
        path.write_text(f'{{"name":"{name}"}}\n', encoding="utf-8")
        path.chmod(0o644)
    database = tmp_path / "traces.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    database.chmod(0o600)
    expected = {
        name: hashlib.sha256((config / name).read_bytes()).hexdigest()
        for name in _CONFIG_NAMES
    }

    serving_runtime_check.pre_update_filesystem_state(
        config,
        database,
        "serving-runtime-public-config-v1",
        expected_sha256=expected,
    )
    (config / "pipeline.json").write_text("{\"drift\":true}\n")
    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="CONFIG_FILE_SHA256",
    ):
        serving_runtime_check.pre_update_filesystem_state(
            config,
            database,
            "serving-runtime-public-config-v1",
            expected_sha256=expected,
        )
    (config / "pipeline.json").unlink()
    (config / "pipeline.json").symlink_to(config / "retrieval.json")
    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="CONFIG_FILE_TYPE",
    ):
        serving_runtime_check.pre_update_filesystem_state(
            config,
            database,
            "serving-runtime-public-config-v1",
            expected_sha256=expected,
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


def test_trace_backup_allows_concurrent_wal_writes_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.sqlite3"
    keeper = sqlite3.connect(source, timeout=10)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute(
        "CREATE TABLE traces (trace_id TEXT PRIMARY KEY, payload TEXT)"
    )
    keeper.executemany(
        "INSERT INTO traces VALUES (?, ?)",
        ((f"trace-{index}", "x" * 4096) for index in range(512)),
    )
    keeper.commit()
    source.chmod(0o600)
    destination = tmp_path / "backup" / "traces-before.sqlite3"
    original_connect = sqlite3.connect
    backup_started = threading.Event()
    writer_finished = threading.Event()

    class _ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def backup(self, target: sqlite3.Connection) -> None:
            signaled = False

            def progress(_status: int, _remaining: int, _total: int) -> None:
                nonlocal signaled
                if not signaled:
                    signaled = True
                    backup_started.set()
                    assert writer_finished.wait(timeout=10)

            self._connection.backup(
                target,
                pages=1,
                progress=progress,
                sleep=0.001,
            )

        def close(self) -> None:
            self._connection.close()

    def connect(
        database: str | Path,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection | _ConnectionProxy:
        connection = original_connect(database, *args, **kwargs)
        if (
            isinstance(database, str)
            and "mode=ro" in database
            and str(source) in database
        ):
            return _ConnectionProxy(connection)
        return connection

    monkeypatch.setattr(serving_runtime_check.sqlite3, "connect", connect)

    def write_during_backup() -> None:
        assert backup_started.wait(timeout=10)
        with original_connect(source, timeout=10) as writer:
            writer.execute(
                "INSERT INTO traces VALUES (?, ?)",
                ("trace-concurrent", "y" * 8192),
            )
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        writer_finished.set()

    writer = threading.Thread(target=write_during_backup)
    writer.start()
    try:
        report = serving_runtime_check.backup_trace_database(
            source,
            destination,
            _REVISION,
        )
    finally:
        writer.join(timeout=10)
        keeper.close()

    assert not writer.is_alive()
    assert report["source_changed_during_backup"] is True
    assert set(report["source_database_identity"]) == {
        "device",
        "file_type",
        "gid",
        "inode",
        "mode",
        "uid",
    }
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == (
            "ok",
        )
        assert connection.execute("SELECT COUNT(*) FROM traces").fetchone()[
            0
        ] >= 512


def test_pre_update_trace_schema_allows_legal_database_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in _CONFIG_NAMES:
        path = config / name
        path.write_text("{}\n", encoding="utf-8")
        path.chmod(0o600)
    database = tmp_path / "traces.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    database.chmod(0o600)
    original = serving_runtime_check.trace_schema

    def trace_schema_with_write(path: Path) -> dict[str, object]:
        value = original(path)
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO traces VALUES ('concurrent')")
        return value

    monkeypatch.setattr(
        serving_runtime_check, "trace_schema", trace_schema_with_write
    )

    report = serving_runtime_check.pre_update_filesystem_state(
        config, database, "first-deploy-private-v1"
    )

    assert report["trace"]["sqlite_user_version"] == 0  # type: ignore[index]


@pytest.mark.parametrize("mutation", ("replace", "mode"))
def test_trace_backup_rejects_stable_source_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    source = tmp_path / "traces.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
        connection.execute("INSERT INTO traces VALUES ('before')")
    source.chmod(0o600)
    destination = tmp_path / "backup" / "traces.sqlite3"
    original_connect = sqlite3.connect

    class _ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def backup(self, target: sqlite3.Connection) -> None:
            self._connection.backup(target)
            if mutation == "mode":
                source.chmod(0o644)
            else:
                replacement = tmp_path / "replacement.sqlite3"
                replacement.write_bytes(source.read_bytes())
                replacement.chmod(0o600)
                replacement.replace(source)

        def close(self) -> None:
            self._connection.close()

    def connect(
        database: str | Path,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection | _ConnectionProxy:
        connection = original_connect(database, *args, **kwargs)
        if (
            isinstance(database, str)
            and "mode=ro" in database
            and str(source) in database
        ):
            return _ConnectionProxy(connection)
        return connection

    monkeypatch.setattr(serving_runtime_check.sqlite3, "connect", connect)

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="SOURCE_MUTATED",
    ):
        serving_runtime_check.backup_trace_database(
            source, destination, _REVISION
        )
    assert not destination.exists()


def test_trace_backup_rejects_owner_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    source.chmod(0o600)
    destination = tmp_path / "backup.sqlite3"
    original = serving_runtime_check._stable_source_identity
    calls = 0

    def drifting_identity(path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        value = original(path)
        if calls > 1:
            value["gid"] = int(value["gid"]) + 1
        return value

    monkeypatch.setattr(
        serving_runtime_check,
        "_stable_source_identity",
        drifting_identity,
    )

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="SOURCE_MUTATED",
    ):
        serving_runtime_check.backup_trace_database(
            source, destination, _REVISION
        )


@pytest.mark.parametrize("source_kind", ("symlink", "directory"))
def test_trace_backup_rejects_symlink_and_non_regular_source(
    tmp_path: Path,
    source_kind: str,
) -> None:
    real = tmp_path / "real.sqlite3"
    with sqlite3.connect(real) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    real.chmod(0o600)
    source = tmp_path / "traces.sqlite3"
    if source_kind == "symlink":
        source.symlink_to(real)
    else:
        source.mkdir(mode=0o600)

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="TRACE_DATABASE_INVALID",
    ):
        serving_runtime_check.backup_trace_database(
            source, tmp_path / "backup.sqlite3", _REVISION
        )


def test_trace_backup_rejects_published_backup_identity_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE traces (trace_id TEXT)")
    source.chmod(0o600)
    destination = tmp_path / "backup.sqlite3"
    original = serving_runtime_check._sqlite_identity
    calls = 0

    def corrupt_second_read(
        connection: sqlite3.Connection,
    ) -> tuple[str, int, int]:
        nonlocal calls
        calls += 1
        value = original(connection)
        if calls == 2:
            return value[0], value[1] + 1, value[2]
        return value

    monkeypatch.setattr(
        serving_runtime_check, "_sqlite_identity", corrupt_second_read
    )

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match="INTEGRITY_FAILED",
    ):
        serving_runtime_check.backup_trace_database(
            source, destination, _REVISION
        )
    assert not destination.exists()


def _write_private_json(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _runtime_contract_files(tmp_path: Path) -> tuple[Path, ...]:
    serving = "sha256:" + "9" * 64
    pre = {
        "active_collection": "rag-docx-active",
        "alias": "rag-industry-active",
        "index_fingerprint": _FINGERPRINT,
        "manifest_sha256": "5" * 64,
        "payload_schema": "industry-pre-update-index-state-v1",
        "point_count": 139,
        "release_revision": _REVISION,
        "source_count": 10,
    }
    trace = {
        "question_capture": "plaintext",
        "question_retention_seconds": 604800,
        "schema_version": 2,
    }
    ui = {
        "allow_insecure_http": True,
        "cookie_secure": False,
        "query_auth_mode": "same_origin_session",
        "session_ttl_seconds": 1800,
    }
    target = {
        "index_fingerprint": _FINGERPRINT,
        "revision": _REVISION,
        "serving_fingerprint": serving,
        "trace": trace,
        "ui": ui,
    }
    manifest = {
        "index_fingerprint": {
            "reindex_required": False,
            "source": _FINGERPRINT,
            "target": _FINGERPRINT,
        },
        "revision": _REVISION,
        "serving_fingerprint": {"source": serving, "target": serving},
        "trace": trace,
        "ui": ui,
    }
    runtime = {
        "active_collection": "rag-docx-active",
        "alias": "rag-industry-active",
        "index_fingerprint": _FINGERPRINT,
        "installed_revision": _REVISION,
        "manifest_sha256": "5" * 64,
        "point_count": 139,
        "production_ready": False,
        "release_matches": True,
        "release_revision": _REVISION,
        "run_mode": "demo",
        "schema_version": "2",
        "serving_fingerprint": serving,
        "trace_question_capture": "plaintext",
        "trace_question_retention_seconds": 604800,
        "trace_schema_version": 2,
        "ui_cookie_secure": False,
        "ui_query_auth_mode": "same_origin_session",
    }
    verified = {
        "index": {
            key: runtime[key]
            for key in (
                "active_collection",
                "alias",
                "index_fingerprint",
                "manifest_sha256",
                "point_count",
            )
        },
        "revision": _REVISION,
        "schema_version": "2",
        "stage": "last_good",
        "update_kind": "serving_app_update",
    }
    return tuple(
        _write_private_json(tmp_path / name, value)
        for name, value in (
            ("pre-index.json", pre),
            ("target-contract.json", target),
            ("verified-state.json", verified),
            ("UPDATE_MANIFEST.json", manifest),
            ("runtime-state.json", runtime),
        )
    )


def test_runtime_state_validation_cross_checks_all_frozen_evidence(
    tmp_path: Path,
) -> None:
    paths = _runtime_contract_files(tmp_path)

    report = serving_runtime_check.validate_runtime_state(*paths)

    assert report == {
        "index_fingerprint": _FINGERPRINT,
        "revision": _REVISION,
        "schema_version": "1",
        "serving_fingerprint": "sha256:" + "9" * 64,
        "verified_state_checked": True,
    }


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("release_revision", "3" * 40, "SERVING_CONTRACT"),
        ("alias", "other-alias", "INDEX_IDENTITY_DRIFT"),
        ("point_count", 138, "INDEX_IDENTITY_DRIFT"),
        ("ui_query_auth_mode", "browser_bearer", "SERVING_CONTRACT"),
        ("trace_question_capture", "hash_only", "SERVING_CONTRACT"),
    ),
)
def test_runtime_state_validation_rejects_target_drift(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    paths = _runtime_contract_files(tmp_path)
    runtime_path = paths[-1]
    runtime = json.loads(runtime_path.read_bytes())
    runtime[field] = value
    _write_private_json(runtime_path, runtime)

    with pytest.raises(
        serving_runtime_check.RuntimeCheckError,
        match=error,
    ):
        serving_runtime_check.validate_runtime_state(*paths)
