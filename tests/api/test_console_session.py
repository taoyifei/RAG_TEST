"""管理员 Cookie Session、CSRF、轮换与退出 API 回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.product_support import build_product_harness


def test_session_cookie_csrf_rotation_and_logout(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        resumed = harness.client.get("/api/v1/console/session")
        assert resumed.status_code == 200
        harness.csrf = str(resumed.json()["csrf_token"])
        denied = harness.client.post(
            "/api/v1/projects",
            headers={"Idempotency-Key": "missing-csrf"},
            json={"name": "禁止写入"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "CSRF_REQUIRED"

        old_cookie = harness.client.cookies.get("rag_console_session")
        rotated = harness.client.post(
            "/api/v1/console/session:rotate",
            headers=harness.write_headers,
        )
        rotated.raise_for_status()
        harness.csrf = str(rotated.json()["csrf_token"])
        new_cookie = harness.client.cookies.get("rag_console_session")

        assert old_cookie != new_cookie
        logged_out = harness.client.delete(
            "/api/v1/console/session", headers=harness.write_headers
        )
        assert logged_out.status_code == 204
        assert harness.client.get("/api/v1/console/session").status_code == 401
    finally:
        harness.close()


def test_bootstrap_failure_is_rate_limited_without_detail(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        harness.client.cookies.clear()
        responses = [
            harness.client.post(
                "/api/v1/console/session",
                json={"bootstrap_token": "x" * 16},
            )
            for _ in range(6)
        ]
        assert all(response.status_code == 403 for response in responses)
        assert all(
            response.json()["error"]["message"] == "管理员登录失败。"
            or response.json()["error"]["message"] == "管理员登录暂不可用。"
            for response in responses
        )
    finally:
        harness.close()


def test_expired_session_cookie_cannot_be_resumed(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE console_sessions SET expires_at=?",
                (expired_at,),
            )

        response = harness.client.get("/api/v1/console/session")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "CONSOLE_SESSION_REQUIRED"
    finally:
        harness.close()
