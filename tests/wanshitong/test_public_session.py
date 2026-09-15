"""WB-03 匿名 Cookie、CSRF 与公共请求边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.core.errors import PolicyDenied
from rag_app.product.crypto import MasterKey
from rag_app.product.provider_runtime import build_offline_mock_transport
from rag_app.wanshitong.public_api import (
    PUBLIC_CAPABILITIES_PATH,
    PUBLIC_CHAT_PATH,
    PUBLIC_FEEDBACK_PATH,
    PUBLIC_SESSION_PATH,
)
from rag_app.wanshitong.public_session import (
    PUBLIC_SESSION_COOKIE,
    PublicSessionService,
)
from tests.product_support import build_product_harness
from tests.wanshitong.support import PublicHarness, public_principal


def test_session_uses_httponly_same_origin_cookie_and_memory_csrf(
    public_harness: PublicHarness,
) -> None:
    response = public_harness.client.post(PUBLIC_SESSION_PATH)

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"session_id", "csrf_token", "expires_in"}
    assert payload["session_id"] == public_harness.session_id
    assert "owner_id" not in payload
    cookie = response.headers["set-cookie"].lower()
    assert f"{PUBLIC_SESSION_COOKIE}=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/public" in cookie
    assert "csrf" not in cookie


def test_anonymous_owners_are_stable_per_sid_and_isolated_between_sids(
    public_harness: PublicHarness,
) -> None:
    first = public_principal(
        public_harness,
        public_harness.client,
        public_harness.csrf,
    )
    resumed = public_harness.client.post(PUBLIC_SESSION_PATH)
    resumed.raise_for_status()
    same = public_principal(
        public_harness,
        public_harness.client,
        str(resumed.json()["csrf_token"]),
    )
    with TestClient(public_harness.app) as other:
        issued = other.post(PUBLIC_SESSION_PATH)
        issued.raise_for_status()
        second = public_principal(
            public_harness, other, str(issued.json()["csrf_token"])
        )

    assert first == same
    assert first.owner_id.startswith("wanshitong-public:")
    assert first.owner_id != "local-admin"
    assert first.owner_id != second.owner_id


def test_session_rejects_tampering_cross_csrf_and_expiration() -> None:
    now = [1_000.0]
    service = PublicSessionService(
        MasterKey(value=b"x" * 32, key_id="synthetic-key"),
        ttl_seconds=60,
        clock=lambda: now[0],
    )
    first = service.issue_or_resume(None)
    second = service.issue_or_resume(None)

    assert (
        service.authenticate(first.cookie_value, first.csrf_token)
        == first.principal
    )
    with pytest.raises(PolicyDenied):
        service.authenticate(first.cookie_value, second.csrf_token)
    with pytest.raises(PolicyDenied):
        service.validate_cookie(first.cookie_value + "tampered")
    now[0] += 60
    with pytest.raises(PolicyDenied, match="已过期"):
        service.validate_cookie(first.cookie_value)


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [({}, 403), ({"X-CSRF-Token": "wrong"}, 403)],
)
def test_public_mutations_require_memory_csrf(
    public_harness: PublicHarness,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    response = public_harness.client.post(
        PUBLIC_CHAT_PATH,
        headers=headers,
        json={"query": "如何办理？"},
    )
    assert response.status_code == expected_status


def test_public_mutation_without_cookie_is_rejected(
    public_harness: PublicHarness,
) -> None:
    with TestClient(public_harness.app) as client:
        response = client.post(
            PUBLIC_FEEDBACK_PATH,
            headers={"X-CSRF-Token": public_harness.csrf},
            json={
                "trace_id": "trace_" + "1" * 32,
                "useful": True,
            },
        )
    assert response.status_code == 401


def test_public_request_models_forbid_identity_and_runtime_overrides(
    public_harness: PublicHarness,
) -> None:
    forbidden = (
        "project_id",
        "knowledge_base_id",
        "owner_id",
        "trace_mode",
        "provider",
        "model",
        "endpoint",
        "retrieval_profile",
        "index_revision_id",
    )
    for field in forbidden:
        response = public_harness.client.post(
            PUBLIC_CHAT_PATH,
            headers=public_harness.headers,
            json={"query": "如何办理？", field: "attacker-controlled"},
        )
        assert response.status_code == 422, field
        query_response = public_harness.client.post(
            f"{PUBLIC_CHAT_PATH}?{field}=attacker-controlled",
            headers=public_harness.headers,
            json={"query": "如何办理？"},
        )
        assert query_response.status_code == 400, field

    session = public_harness.client.post(
        PUBLIC_SESSION_PATH, json={"owner_id": "attacker-controlled"}
    )
    feedback = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={
            "trace_id": "trace_" + "1" * 32,
            "useful": True,
            "project_id": "attacker-controlled",
        },
    )
    assert session.status_code == 422
    assert feedback.status_code == 422


@pytest.mark.parametrize("query", ["", " \n\t"])
def test_public_chat_rejects_blank_queries(
    public_harness: PublicHarness, query: str
) -> None:
    response = public_harness.client.post(
        PUBLIC_CHAT_PATH,
        headers=public_harness.headers,
        json={"query": query},
    )
    assert response.status_code == 422


def test_public_routes_are_disabled_in_universal_mode(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        response = harness.client.get(PUBLIC_CAPABILITIES_PATH)
        assert response.status_code == 404
    finally:
        harness.close()


def test_wanshitong_mode_requires_product_master_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>湾事通</title>", encoding="utf-8"
    )
    bootstrap = tmp_path / "bootstrap-token"
    bootstrap.write_text(
        "synthetic-wanshitong-bootstrap-token", encoding="utf-8"
    )
    bootstrap.chmod(0o600)
    runtime = build_product_runtime(
        ProductRuntimeSettings(
            data_dir=tmp_path / "data",
            frontend_dir=frontend,
            bootstrap_token_file=bootstrap,
            master_key_file=None,
            qdrant_mode="memory",
        ),
        transport_factory=build_offline_mock_transport,
        recover_jobs=False,
    )
    try:
        with pytest.raises(ValueError, match="RAG_MASTER_KEY_FILE"):
            create_product_app(runtime)
        assert runtime.sdk.list_projects() == ()
    finally:
        runtime.close()
