"""湾事通私网 HTTP 演示模式的精确边界测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.wanshitong.public_api import PUBLIC_SESSION_PATH
from tests.product_support import build_product_harness

_HTTP_ORIGIN = "http://10.242.180.60:8288"


def test_universal_mode_never_enables_private_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAG_PRODUCT_MODE", raising=False)
    monkeypatch.setenv("RAG_WANSHITONG_DEMO_ALLOW_HTTP", "true")
    harness = build_product_harness(tmp_path)
    harness.runtime.settings = replace(
        harness.runtime.settings, trusted_origins=(_HTTP_ORIGIN,)
    )
    client = TestClient(
        create_product_app(harness.runtime),
        base_url=_HTTP_ORIGIN,
        client=("10.242.180.54", 41000),
    )
    try:
        response = client.get("/live")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TLS_REQUIRED"
    finally:
        client.close()
        harness.close()


def test_wanshitong_private_http_cookies_work_for_all_session_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    monkeypatch.setenv("RAG_WANSHITONG_DEMO_ALLOW_HTTP", "true")
    harness = build_product_harness(tmp_path)
    harness.runtime.settings = replace(
        harness.runtime.settings, trusted_origins=(_HTTP_ORIGIN,)
    )
    client = TestClient(
        create_product_app(harness.runtime),
        base_url=_HTTP_ORIGIN,
        client=("10.242.180.54", 41000),
    )
    try:
        live = client.get("/live")
        login = client.post(
            "/api/v1/console/session",
            headers={"Origin": _HTTP_ORIGIN},
            json={"bootstrap_token": harness.bootstrap_token},
        )
        login.raise_for_status()
        resumed = client.get("/api/v1/console/session")
        resumed.raise_for_status()
        rotated = client.post(
            "/api/v1/console/session:rotate",
            headers={"X-CSRF-Token": resumed.json()["csrf_token"]},
        )
        rotated.raise_for_status()
        public_session = client.post(PUBLIC_SESSION_PATH)
        public_session.raise_for_status()

        assert live.status_code == 200
        for response in (login, resumed, rotated, public_session):
            assert "Secure" not in response.headers["set-cookie"]
    finally:
        client.close()
        harness.close()


@pytest.mark.parametrize(
    ("base_url", "peer", "origin"),
    [
        ("http://10.242.180.60:8388", "10.242.180.54", None),
        (_HTTP_ORIGIN, "203.0.113.9", None),
        (_HTTP_ORIGIN, "10.242.180.54", "http://10.242.180.54:18288"),
    ],
)
def test_wanshitong_private_http_rejects_untrusted_request_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    peer: str,
    origin: str | None,
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    monkeypatch.setenv("RAG_WANSHITONG_DEMO_ALLOW_HTTP", "true")
    harness = build_product_harness(tmp_path)
    harness.runtime.settings = replace(
        harness.runtime.settings, trusted_origins=(_HTTP_ORIGIN,)
    )
    client = TestClient(
        create_product_app(harness.runtime),
        base_url=base_url,
        client=(peer, 41000),
    )
    headers = {} if origin is None else {"Origin": origin}
    try:
        response = client.get("/live", headers=headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TLS_REQUIRED"
    finally:
        client.close()
        harness.close()


def test_https_always_uses_secure_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = "https://rag.internal.example"
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    monkeypatch.setenv("RAG_WANSHITONG_DEMO_ALLOW_HTTP", "true")
    harness = build_product_harness(tmp_path)
    harness.runtime.settings = replace(
        harness.runtime.settings, trusted_origins=(origin,)
    )
    client = TestClient(
        create_product_app(harness.runtime),
        base_url=origin,
        client=("203.0.113.9", 41000),
    )
    try:
        login = client.post(
            "/api/v1/console/session",
            headers={"Origin": origin},
            json={"bootstrap_token": harness.bootstrap_token},
        )
        public_session = client.post(PUBLIC_SESSION_PATH)

        login.raise_for_status()
        public_session.raise_for_status()
        assert "Secure" in login.headers["set-cookie"]
        assert "Secure" in public_session.headers["set-cookie"]
    finally:
        client.close()
        harness.close()
