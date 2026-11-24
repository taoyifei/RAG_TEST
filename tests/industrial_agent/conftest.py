"""为网关权限测试显式配置合成管理账号。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def configure_test_administrators(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离测试角色，生产部署自行指定账号及超级管理员 ID。"""
    monkeypatch.setenv("INDUSTRIAL_AGENT_ADMIN_USERNAMES", "example_admin")
    monkeypatch.setenv("INDUSTRIAL_AGENT_SUPER_ADMIN_USER_IDS", "900001")
