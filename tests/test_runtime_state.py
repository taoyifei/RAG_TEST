from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from rag_app import cli
from rag_app.settings import RunMode, UiQueryAuthMode
from rag_app.tracing.models import TraceQuestionCapture

_REVISION = "a" * 40
_INDEX_FINGERPRINT = "sha256:" + "b" * 64
_SERVING_FINGERPRINT = "sha256:" + "c" * 64


def test_runtime_state_reports_serving_ui_trace_and_revision_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime-state v2 必须足以证明 serving contract 实际生效。"""
    settings = SimpleNamespace(
        admin_token=SecretStr("d" * 32),
        intent_router_calibration_path="calibration.json",
        intent_router_path="router.json",
        manifest_database="manifest.sqlite3",
        pipeline_path="pipeline.json",
        qdrant_alias="rag-industry-active",
        qdrant_api_key=SecretStr("e" * 32),
        qdrant_url="http://qdrant:6333",
        release_revision=_REVISION,
        retrieval_path="retrieval.json",
        run_mode=RunMode.DEMO,
        trace_question_capture=TraceQuestionCapture.PLAINTEXT,
        trace_question_retention_seconds=604_800,
        trace_database="traces.sqlite3",
        ui_cookie_secure=False,
        ui_query_auth_mode=UiQueryAuthMode.SAME_ORIGIN_SESSION,
    )
    pipeline = SimpleNamespace(fingerprint=lambda: _INDEX_FINGERPRINT)
    active = SimpleNamespace(
        manifest=SimpleNamespace(
            collection_name="rag-docx-active",
            pipeline_fingerprint=_INDEX_FINGERPRINT,
        ),
        manifest_sha256="f" * 64,
    )

    class _Repository:
        def __init__(self, _path: str) -> None:
            pass

        def get_active(self) -> object:
            return active

    class _Qdrant:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_aliases(self) -> object:
            return SimpleNamespace(
                aliases=[
                    SimpleNamespace(
                        alias_name="rag-industry-active",
                        collection_name="rag-docx-active",
                    )
                ]
            )

        def count(self, _collection: str, *, exact: bool) -> object:
            assert exact is True
            return SimpleNamespace(count=139)

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "RuntimeSettings", lambda: settings)
    monkeypatch.setattr(cli, "require_release_revision", lambda _value: None)
    monkeypatch.setattr(cli, "load_pipeline", lambda _path: pipeline)
    monkeypatch.setattr(cli, "ReadOnlyManifestRepository", _Repository)
    monkeypatch.setattr(cli, "QdrantClient", _Qdrant)
    monkeypatch.setattr(
        cli,
        "build_serving_fingerprint",
        lambda _settings: _SERVING_FINGERPRINT,
        raising=False,
    )
    monkeypatch.setattr(cli, "_trace_schema_version", lambda _path: 2)
    monkeypatch.setattr(cli, "SOURCE_REVISION", _REVISION)

    state = cli._runtime_state()

    assert state == {
        "active_collection": "rag-docx-active",
        "alias": "rag-industry-active",
        "index_fingerprint": _INDEX_FINGERPRINT,
        "installed_revision": _REVISION,
        "manifest_sha256": "f" * 64,
        "point_count": 139,
        "production_ready": False,
        "release_matches": True,
        "release_revision": _REVISION,
        "run_mode": "demo",
        "schema_version": "2",
        "serving_fingerprint": _SERVING_FINGERPRINT,
        "trace_question_capture": "plaintext",
        "trace_question_retention_seconds": 604_800,
        "trace_schema_version": 2,
        "ui_cookie_secure": False,
        "ui_query_auth_mode": "same_origin_session",
    }


@pytest.mark.parametrize(
    ("point_count", "serving_fingerprint"),
    (
        (0, _SERVING_FINGERPRINT),
        (True, _SERVING_FINGERPRINT),
        (139, "sha256:invalid"),
    ),
)
def test_runtime_state_rejects_invalid_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
    point_count: object,
    serving_fingerprint: str,
) -> None:
    settings = SimpleNamespace(
        intent_router_calibration_path="calibration.json",
        intent_router_path="router.json",
        manifest_database="manifest.sqlite3",
        pipeline_path="pipeline.json",
        qdrant_alias="rag-industry-active",
        qdrant_api_key=SecretStr("e" * 32),
        qdrant_url="http://qdrant:6333",
        release_revision=_REVISION,
        retrieval_path="retrieval.json",
        run_mode=RunMode.DEMO,
        trace_question_capture=TraceQuestionCapture.PLAINTEXT,
        trace_question_retention_seconds=604_800,
        trace_database="traces.sqlite3",
        ui_cookie_secure=False,
        ui_query_auth_mode=UiQueryAuthMode.SAME_ORIGIN_SESSION,
    )
    pipeline = SimpleNamespace(fingerprint=lambda: _INDEX_FINGERPRINT)
    active = SimpleNamespace(
        manifest=SimpleNamespace(
            collection_name="rag-docx-active",
            pipeline_fingerprint=_INDEX_FINGERPRINT,
        ),
        manifest_sha256="f" * 64,
    )

    class _Repository:
        def __init__(self, _path: str) -> None:
            pass

        def get_active(self) -> object:
            return active

    class _Qdrant:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_aliases(self) -> object:
            return SimpleNamespace(
                aliases=[
                    SimpleNamespace(
                        alias_name="rag-industry-active",
                        collection_name="rag-docx-active",
                    )
                ]
            )

        def count(self, _collection: str, *, exact: bool) -> object:
            assert exact is True
            return SimpleNamespace(count=point_count)

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "RuntimeSettings", lambda: settings)
    monkeypatch.setattr(cli, "require_release_revision", lambda _value: None)
    monkeypatch.setattr(cli, "load_pipeline", lambda _path: pipeline)
    monkeypatch.setattr(cli, "ReadOnlyManifestRepository", _Repository)
    monkeypatch.setattr(cli, "QdrantClient", _Qdrant)
    monkeypatch.setattr(
        cli,
        "build_serving_fingerprint",
        lambda _settings: serving_fingerprint,
    )
    monkeypatch.setattr(cli, "_trace_schema_version", lambda _path: 2)
    monkeypatch.setattr(cli, "SOURCE_REVISION", _REVISION)

    with pytest.raises(ValueError, match="runtime-state"):
        cli._runtime_state()


def test_trace_schema_version_reads_actual_sqlite_state(tmp_path: Path) -> None:
    path = tmp_path / "traces.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE traces ("
            "trace_id TEXT, question_text TEXT, question_sha256 TEXT)"
        )
        connection.execute("PRAGMA user_version=2")

    assert cli._trace_schema_version(path) == 2
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="Trace schema"):
        cli._trace_schema_version(path)
