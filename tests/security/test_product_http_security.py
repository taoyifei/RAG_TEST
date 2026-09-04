"""P11 Product HTTP 安全响应、TLS、Origin 与限流回归。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from tests.product_support import (
    build_product_harness,
    create_provider_connections,
)


def test_security_headers_origin_and_secure_cookie(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        live = harness.client.get("/live")
        assert live.headers["x-content-type-options"] == "nosniff"
        assert live.headers["referrer-policy"] == "no-referrer"
        assert live.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in live.headers[
            "content-security-policy"
        ]

        denied = harness.client.post(
            "/api/v1/console/session",
            headers={"Origin": "https://attacker.invalid"},
            json={"bootstrap_token": harness.bootstrap_token},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ORIGIN_DENIED"

        harness.runtime.settings = replace(
            harness.runtime.settings,
            trusted_origins=("https://rag.example.com",),
        )
        secure_client = TestClient(
            create_product_app(harness.runtime),
            base_url="https://rag.example.com",
        )
        try:
            login = secure_client.post(
                "/api/v1/console/session",
                headers={"Origin": "https://rag.example.com"},
                json={"bootstrap_token": harness.bootstrap_token},
            )
            login.raise_for_status()
            cookie = login.headers["set-cookie"]
            assert "HttpOnly" in cookie
            assert "Secure" in cookie
            assert "SameSite=lax" in cookie
            assert "max-age=31536000" in login.headers[
                "strict-transport-security"
            ]
        finally:
            secure_client.close()
    finally:
        harness.close()


def test_non_loopback_requires_tls_and_trusts_only_named_proxy(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        direct = TestClient(
            create_product_app(harness.runtime),
            base_url="http://rag.example.com",
            client=("10.0.0.2", 41000),
        )
        try:
            denied = direct.get(
                "/live", headers={"X-Forwarded-Proto": "https"}
            )
            assert denied.status_code == 400
            assert denied.json()["error"]["code"] == "TLS_REQUIRED"
        finally:
            direct.close()

        spoofed_host = TestClient(
            create_product_app(harness.runtime),
            base_url="http://localhost",
            client=("10.0.0.2", 41000),
        )
        try:
            denied = spoofed_host.get("/live")
            assert denied.status_code == 400
            assert denied.json()["error"]["code"] == "TLS_REQUIRED"
        finally:
            spoofed_host.close()

        harness.runtime.settings = replace(
            harness.runtime.settings,
            trusted_proxies=frozenset({"10.0.0.2"}),
        )
        proxied = TestClient(
            create_product_app(harness.runtime),
            base_url="http://rag.example.com",
            client=("10.0.0.2", 41000),
        )
        try:
            accepted = proxied.get(
                "/live", headers={"X-Forwarded-Proto": "https"}
            )
            assert accepted.status_code == 200
            assert "max-age=31536000" in accepted.headers[
                "strict-transport-security"
            ]
        finally:
            proxied.close()
    finally:
        harness.close()


def test_provider_query_and_upload_routes_are_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, _, jina_connection, _ = create_provider_connections(harness)
        provider_responses = [
            harness.client.post(
                f"/api/v1/provider-connections/{jina_connection}:validate",
                headers=harness.write_headers,
                json={
                    "operation": "embedding.query",
                    "model": "jina-embeddings-v5-text-small",
                    "expected_dimension": 1024,
                },
            )
            for _ in range(6)
        ]
        query_responses = [
            harness.client.post(
                "/api/v1/projects/prj_missing/"
                "knowledge-bases/kb_missing:search",
                headers=harness.write_headers,
                json={"query": "公开合成问题", "limit": 1},
            )
            for _ in range(61)
        ]
        upload_responses = [
            harness.client.post(
                "/api/v1/projects/prj_missing/"
                "knowledge-bases/kb_missing/documents",
                headers={
                    **harness.write_headers,
                    "Content-Type": (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    "Idempotency-Key": f"missing-{index}",
                },
                content=b"synthetic",
            )
            for index in range(11)
        ]

        assert all(item.status_code != 429 for item in provider_responses[:5])
        assert provider_responses[-1].status_code == 429
        assert all(item.status_code != 429 for item in query_responses[:60])
        assert query_responses[-1].status_code == 429
        assert all(item.status_code != 429 for item in upload_responses[:10])
        assert upload_responses[-1].status_code == 429
        assert upload_responses[-1].headers["retry-after"] == "60"
    finally:
        harness.close()
