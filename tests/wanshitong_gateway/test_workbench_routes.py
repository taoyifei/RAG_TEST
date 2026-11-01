"""验证工作台路由使用员工身份、限制请求并正确释放原生事件流。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.weknora.admin import (
    NativeAdminResponse,
    NativeAdminStream,
)
from wanshitong_gateway.workbench import WorkbenchService
from wanshitong_gateway.workbench_routes import register_workbench_routes


class _EmployeeAuth:
    def require_profile(
        self, request: Request, *, csrf: bool | None = None
    ) -> UserProfile:
        if request.headers.get("X-Employee") != "138":
            raise HTTPException(401, "employee required")
        needs_csrf = csrf is True or (
            csrf is None and request.method not in {"GET", "HEAD"}
        )
        if (
            needs_csrf
            and request.headers.get("X-CSRF-Token") != "synthetic-csrf"
        ):
            raise HTTPException(403, "csrf required")
        return UserProfile("138", "luoyang", "普通用户", "208", "user")


class _Stream:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.read()

    async def read(self) -> AsyncIterator[bytes]:
        yield b'event: complete\ndata: {"done":true}\n\n'

    async def aclose(self) -> None:
        self.closed = True


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.proxied = 0
        self.stream = _Stream()

    async def bootstrap(
        self, profile: UserProfile, scopes: tuple[str, ...]
    ) -> dict:
        self.calls.append((profile.user_id, scopes))
        return {"workspace_id": str(uuid4()), "search_scopes": list(scopes)}

    async def proxy(self, **kwargs: object) -> NativeAdminResponse:
        self.proxied += 1
        assert isinstance(kwargs["profile"], UserProfile)
        if kwargs["method"] == "GET":
            return NativeAdminResponse(
                200, b'{"success":true}', {"content-type": "application/json"}
            )
        assert kwargs["method"] == "POST"
        return NativeAdminResponse(
            200,
            b"",
            {"content-type": "text/event-stream"},
            cast(NativeAdminStream, self.stream),
        )


_HEADERS = {"X-Employee": "138", "X-CSRF-Token": "synthetic-csrf"}


@pytest.fixture
def endpoint() -> Iterator[tuple[TestClient, _Service]]:
    service = _Service()
    app = FastAPI(root_path="/kb")
    register_workbench_routes(
        app, cast(GatewayAuth, _EmployeeAuth()), cast(WorkbenchService, service)
    )
    with TestClient(app) as client:
        yield client, service


def test_bootstrap_is_employee_only_and_defaults_to_public(
    endpoint: tuple[TestClient, _Service],
) -> None:
    client, service = endpoint
    assert (
        client.post("/kb/api/public/workbench/bootstrap", json={}).status_code
        == 401
    )
    assert (
        client.post(
            "/kb/api/public/workbench/bootstrap",
            json={},
            headers={"X-Employee": "138"},
        ).status_code
        == 403
    )
    result = client.post(
        "/kb/api/public/workbench/bootstrap", json={}, headers=_HEADERS
    )
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert service.calls == [("138", ("public",))]


@pytest.mark.parametrize(
    ("scopes", "normalized"),
    [
        (["hr_public"], ("hr_public",)),
        (["hr_public", "personal"], ("hr_public", "personal")),
        (["hr_public", "public"], ("public",)),
    ],
)
def test_bootstrap_accepts_hr_public_and_collapses_covered_scope(
    endpoint: tuple[TestClient, _Service],
    scopes: list[str],
    normalized: tuple[str, ...],
) -> None:
    client, service = endpoint
    response = client.post(
        "/kb/api/public/workbench/bootstrap",
        json={"search_scopes": scopes},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert service.calls == [("138", normalized)]


@pytest.mark.parametrize(
    "body",
    [
        {"search_scopes": []},
        {"search_scopes": ["other"]},
        {"search_scopes": ["public"], "tenant_id": 10000},
    ],
)
def test_bootstrap_rejects_empty_or_arbitrary_native_scope(
    endpoint: tuple[TestClient, _Service], body: dict
) -> None:
    client, service = endpoint
    assert (
        client.post(
            "/kb/api/public/workbench/bootstrap", json=body, headers=_HEADERS
        ).status_code
        == 422
    )
    assert service.calls == []


def test_native_stream_is_not_buffered_and_closes_on_completion(
    endpoint: tuple[TestClient, _Service],
) -> None:
    client, service = endpoint
    response = client.post(
        f"/kb/api/workbench/{uuid4()}/native/api/v1/agent-chat/session-1",
        json={"query": "整理资料"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert "event: complete" in response.text
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-store"
    assert service.stream.closed


def test_disabled_workbench_does_not_provision_native_identity() -> None:
    app = FastAPI(root_path="/kb")
    register_workbench_routes(app, cast(GatewayAuth, _EmployeeAuth()), None)
    with TestClient(app) as client:
        response = client.post(
            "/kb/api/public/workbench/bootstrap", json={}, headers=_HEADERS
        )
    assert response.status_code == 503


def test_native_request_size_is_checked_before_forwarding(
    endpoint: tuple[TestClient, _Service],
) -> None:
    client, service = endpoint
    response = client.post(
        f"/kb/api/workbench/{uuid4()}/native/api/v1/agent-chat/session-1",
        content=b"x" * (128 * 1024 + 1),
        headers=_HEADERS,
    )
    assert response.status_code == 413
    assert service.proxied == 0


def test_native_safe_read_uses_cookie_identity_without_write_csrf(
    endpoint: tuple[TestClient, _Service],
) -> None:
    client, service = endpoint
    result = client.get(
        f"/kb/api/workbench/{uuid4()}/native/api/v1/auth/me",
        headers={"X-Employee": "138"},
    )
    assert result.status_code == 200
    assert result.json() == {"success": True}
    assert service.proxied == 1
