"""显式 demo 运行模式的安全边界。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rag_app.api.app import ApiServices, create_app
from rag_app.health import (
    ComponentStatus,
    FrozenConfigurationProbe,
    ReadinessService,
)
from rag_app.runtime import load_pipeline, log_run_mode_startup
from rag_app.settings import RetrievalSettings, RunMode, RuntimeSettings
from rag_app.worker_runtime import require_indexable_configuration


@dataclass(frozen=True, slots=True)
class _ReadyProbe:
    def check(self) -> ComponentStatus:
        return ComponentStatus("dependencies", True, "ready", 1, 1)


def _runtime_values(tmp_path: Path) -> dict[str, object]:
    return {
        "access_mode": "shared_corpus",
        "query_token": uuid.uuid4().hex,
        "admin_token": uuid.uuid4().hex,
        "qdrant_api_key": uuid.uuid4().hex,
        "qdrant_url": "http://rag-qdrant:6333",
        "qdrant_alias": "rag-docx-active",
        "release_revision": "1" * 40,
        "state_database": tmp_path / "state.sqlite3",
        "manifest_database": tmp_path / "manifest.sqlite3",
        "pipeline_path": tmp_path / "pipeline.json",
        "retrieval_path": tmp_path / "retrieval.json",
        "frontend_dir": tmp_path / "frontend",
        "llm_tokenizer_path": tmp_path / "tokenizer.json",
        "embedding_endpoints": '["http://embedding:80"]',
        "reranker_endpoints": '["http://reranker:80"]',
        "llm_endpoints": '["http://llm:8000"]',
        "embedding_model": "Qwen3-Embedding-0.6B",
        "reranker_model": "Qwen3-Reranker-0.6B",
        "llm_model": "Qwen/Qwen3-8B-AWQ",
    }


def test_run_mode_defaults_to_production_and_rejects_unknown(
    tmp_path: Path,
) -> None:
    values = _runtime_values(tmp_path)

    assert RuntimeSettings(**values).run_mode is RunMode.PRODUCTION
    values["run_mode"] = "demo"
    assert RuntimeSettings(**values).run_mode is RunMode.DEMO
    values["run_mode"] = "smoke"
    with pytest.raises(ValidationError, match="run_mode"):
        RuntimeSettings(**values)
    production_compose = (
        Path(__file__).resolve().parents[1] / "deployment/compose.yaml"
    ).read_text(encoding="utf-8")
    assert "RAG_RUN_MODE" not in production_compose


def test_demo_allows_provisional_without_weakening_production() -> None:
    root = Path(__file__).resolve().parents[1]
    pipeline = load_pipeline(root / "deployment/config/pipeline.json")
    retrieval = RetrievalSettings.load(
        root / "deployment/config/retrieval.json"
    )

    with pytest.raises(ValueError, match="冻结集"):
        require_indexable_configuration(
            pipeline,
            retrieval,
            None,
            run_mode=RunMode.PRODUCTION,
        )
    require_indexable_configuration(
        pipeline,
        retrieval,
        None,
        run_mode=RunMode.DEMO,
    )
    assert FrozenConfigurationProbe(retrieval).check().ready is False
    assert (
        FrozenConfigurationProbe(
            retrieval,
            allow_provisional=True,
        ).check().ready
        is True
    )


def test_demo_ready_response_is_never_presented_as_production() -> None:
    readiness = ReadinessService((_ReadyProbe(),))
    readiness.refresh_once()
    demo_services = ApiServices(
        readiness=readiness,
        query_token=uuid.uuid4().hex,
        admin_token=uuid.uuid4().hex,
        run_mode=RunMode.DEMO,
    )

    response = TestClient(create_app(demo_services)).get("/ready")

    assert response.status_code == 200
    assert response.json()["run_mode"] == "demo"
    assert response.json()["production_ready"] is False

    production_services = ApiServices(
        readiness=readiness,
        query_token=uuid.uuid4().hex,
        admin_token=uuid.uuid4().hex,
    )
    production_payload = TestClient(
        create_app(production_services)
    ).get("/ready").json()
    assert "run_mode" not in production_payload
    assert "production_ready" not in production_payload


def test_demo_startup_log_uses_stable_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="rag_app.run_mode"):
        log_run_mode_startup(RunMode.DEMO, component="rag-app")

    assert caplog.messages == ["DEMO_MODE_ACTIVE component=rag-app"]

    caplog.clear()
    log_run_mode_startup(RunMode.PRODUCTION, component="rag-app")
    assert caplog.messages == []
