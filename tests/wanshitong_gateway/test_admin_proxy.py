"""白标原生管理界面所用 API 与账号边界。"""

from __future__ import annotations

import pytest

from wanshitong_gateway.weknora.admin import _allowed_admin_path


@pytest.mark.parametrize(
    "path",
    [
        "api/v1/me/browser",
        "api/v1/user/favorites",
        "api/v1/organizations",
        "api/v1/tenants/kv/retrieval-config",
        "api/v1/web-search-providers",
        "api/v1/im-channels",
        "api/v1/embed-channels",
        "api/v1/knowledge-bases",
        "api/v1/models",
    ],
)
def test_native_admin_ui_routes_are_reachable(path: str) -> None:
    assert _allowed_admin_path("GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "api/v1/auth/login",
        "api/v1/system/admin/users",
        "api/v1/system/host-project-dir",
        "api/v1/initialization/ollama/models/download",
        "api/v1/knowledge-bases/../auth/login",
    ],
)
def test_native_admin_proxy_keeps_account_and_host_boundaries(
    path: str,
) -> None:
    assert not _allowed_admin_path("POST", path)
