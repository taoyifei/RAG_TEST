"""WB-01 湾事通固定 Scope 的启动与鉴权门禁。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import (
    ProductRuntime,
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.product.crypto import initialize_master_key
from rag_app.product.provider_runtime import build_offline_mock_transport
from rag_app.wanshitong.api import SCOPE_STATUS_PATH
from rag_app.wanshitong.errors import ScopeBindingError
from rag_app.wanshitong.mode import (
    WANSHITONG_KNOWLEDGE_BASE_NAME,
    WANSHITONG_PROJECT_NAME,
    WANSHITONG_SCOPE_KEY,
)
from rag_app.wanshitong.models import ScopeBinding
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.scope_store import ScopeBindingStore


def _runtime_settings(tmp_path: Path) -> ProductRuntimeSettings:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True, exist_ok=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>湾事通测试</title>", encoding="utf-8"
    )
    bootstrap_token = tmp_path / "bootstrap-token"
    bootstrap_token.write_text(
        "synthetic-wanshitong-bootstrap-token", encoding="utf-8"
    )
    bootstrap_token.chmod(0o600)
    master_key = tmp_path / "master-key"
    initialize_master_key(master_key)
    return ProductRuntimeSettings(
        data_dir=tmp_path / "data",
        frontend_dir=frontend,
        bootstrap_token_file=bootstrap_token,
        master_key_file=master_key,
        qdrant_mode="memory",
    )


def _build_runtime(settings: ProductRuntimeSettings) -> ProductRuntime:
    return build_product_runtime(
        settings,
        transport_factory=build_offline_mock_transport,
        recover_jobs=False,
    )


def test_first_wanshitong_start_creates_and_binds_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    runtime = _build_runtime(_runtime_settings(tmp_path))
    try:
        assert runtime.sdk.list_projects() == ()
        create_product_app(runtime)

        binding = ScopeBindingStore(runtime.connections).read()
        assert binding is not None
        project = runtime.sdk.get_project(binding.project_id)
        knowledge_base = runtime.sdk.get_knowledge_base(
            binding.project_id, binding.knowledge_base_id
        )
        assert binding.system_key == WANSHITONG_SCOPE_KEY
        assert project.name == WANSHITONG_PROJECT_NAME
        assert knowledge_base.name == WANSHITONG_KNOWLEDGE_BASE_NAME
        assert knowledge_base.project_id == project.project_id
    finally:
        runtime.close()


def test_restart_reuses_persisted_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    settings = _runtime_settings(tmp_path)
    first_runtime = _build_runtime(settings)
    try:
        create_product_app(first_runtime)
        first_binding = ScopeBindingStore(first_runtime.connections).read()
        assert first_binding is not None
    finally:
        first_runtime.close()

    second_runtime = _build_runtime(settings)
    try:
        create_product_app(second_runtime)
        second_binding = ScopeBindingStore(second_runtime.connections).read()
        assert second_binding == first_binding
        assert len(second_runtime.sdk.list_projects()) == 1
        assert (
            len(second_runtime.sdk.list_knowledge_bases(first_binding.project_id))
            == 1
        )
    finally:
        second_runtime.close()


def test_concurrent_ensure_does_not_duplicate_scope(tmp_path: Path) -> None:
    runtime = _build_runtime(_runtime_settings(tmp_path))
    service = FixedScopeService(
        runtime.sdk, ScopeBindingStore(runtime.connections)
    )
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            bindings = tuple(
                executor.map(lambda _: service.ensure(), range(16))
            )

        identities = {
            (binding.project_id, binding.knowledge_base_id)
            for binding in bindings
        }
        assert len(identities) == 1
        assert len(runtime.sdk.list_projects()) == 1
        binding = bindings[0]
        assert len(runtime.sdk.list_knowledge_bases(binding.project_id)) == 1
    finally:
        runtime.close()


def test_invalid_parent_binding_blocks_without_creating_scope(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(_runtime_settings(tmp_path))
    store = ScopeBindingStore(runtime.connections)
    service = FixedScopeService(runtime.sdk, store)
    try:
        valid = service.ensure()
        other_project = runtime.sdk.create_project(
            "错误绑定目标",
            idempotency_key="wanshitong-invalid-binding-project",
        )
        corrupted = ScopeBinding(
            project_id=other_project.project_id,
            knowledge_base_id=valid.knowledge_base_id,
            created_at=valid.created_at,
        )
        connection = runtime.connections.connect()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE metadata SET value=? WHERE namespace=? AND key=?",
                (
                    corrupted.model_dump_json(),
                    "wanshitong.fixed-scope",
                    WANSHITONG_SCOPE_KEY,
                ),
            )
        finally:
            connection.close()
        projects_before = runtime.sdk.list_projects()

        with pytest.raises(ScopeBindingError, match="指向无效对象"):
            service.ensure()

        assert runtime.sdk.list_projects() == projects_before
        assert runtime.sdk.list_knowledge_bases(other_project.project_id) == ()
    finally:
        runtime.close()


@pytest.mark.parametrize("configured_mode", [None, "universal"])
def test_universal_mode_does_not_create_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_mode: str | None,
) -> None:
    if configured_mode is None:
        monkeypatch.delenv("RAG_PRODUCT_MODE", raising=False)
    else:
        monkeypatch.setenv("RAG_PRODUCT_MODE", configured_mode)
    runtime = _build_runtime(_runtime_settings(tmp_path))
    try:
        create_product_app(runtime)
        assert runtime.sdk.list_projects() == ()
        assert ScopeBindingStore(runtime.connections).read() is None
    finally:
        runtime.close()


def test_scope_status_api_requires_admin_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    runtime = _build_runtime(_runtime_settings(tmp_path))
    query_token = "-".join(("synthetic", "query", "token"))
    admin_token = "-".join(("synthetic", "admin", "token"))
    app = create_product_app(
        runtime,
        query_token=query_token,
        admin_token=admin_token,
    )
    try:
        with TestClient(app) as client:
            unauthenticated = client.get(SCOPE_STATUS_PATH)
            query_only = client.get(
                SCOPE_STATUS_PATH,
                headers={"Authorization": f"Bearer {query_token}"},
            )
            administrator = client.get(
                SCOPE_STATUS_PATH,
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert unauthenticated.status_code == 401
        assert query_only.status_code == 403
        assert administrator.status_code == 200
        payload = administrator.json()
        assert payload["ready"] is True
        assert payload["system_key"] == WANSHITONG_SCOPE_KEY
        assert payload["project_name"] == WANSHITONG_PROJECT_NAME
        assert (
            payload["knowledge_base_name"]
            == WANSHITONG_KNOWLEDGE_BASE_NAME
        )
    finally:
        runtime.close()
