"""WB-06 快捷入口与公共空筛选脚手架门禁。"""

from __future__ import annotations

import pytest

from rag_app.wanshitong import public_api
from rag_app.wanshitong.public_api import (
    PUBLIC_CAPABILITIES_PATH,
    PUBLIC_CHAT_PATH,
)
from rag_app.wanshitong.shortcuts import (
    DRAFT_SHORTCUTS,
    SHORTCUT_CATALOG,
    PublicMetadataFilter,
    current_public_filter,
)
from tests.wanshitong.support import PublicHarness


def test_all_draft_shortcuts_are_disabled_invisible_and_server_owned() -> None:
    assert tuple(item.shortcut_id for item in DRAFT_SHORTCUTS) == (
        "policy",
        "process",
        "hr-service",
        "technical",
    )
    assert all(
        not item.enabled and not item.visible for item in DRAFT_SHORTCUTS
    )
    assert SHORTCUT_CATALOG.public_definitions() == ()
    assert all(
        "allowed_roles" not in type(item.filter_expression).model_fields
        and "allowed_groups" not in type(item.filter_expression).model_fields
        for item in DRAFT_SHORTCUTS
    )
    with pytest.raises(KeyError):
        SHORTCUT_CATALOG.resolve_filter("policy")


def test_public_capabilities_exposes_no_shortcuts(
    public_harness: PublicHarness,
) -> None:
    response = public_harness.client.get(PUBLIC_CAPABILITIES_PATH)

    assert response.status_code == 200
    assert response.json()["shortcuts"] == []


@pytest.mark.parametrize(
    "extra",
    [
        {"department_keys": ["01-科管"]},
        {"category_prefixes": [["02 科研项目管理"]]},
        {"topic_keys": ["policy"]},
        {"shortcut_id": "policy"},
        {"filter_expression": {"topic_keys": ["policy"]}},
    ],
)
def test_public_chat_rejects_all_client_filter_fields(
    public_harness: PublicHarness, extra: dict[str, object]
) -> None:
    response = public_harness.client.post(
        PUBLIC_CHAT_PATH,
        headers=public_harness.headers,
        json={"query": "合成问题", **extra},
    )

    assert response.status_code == 422


def test_current_public_filter_is_forced_empty() -> None:
    metadata_filter = current_public_filter()

    assert metadata_filter == PublicMetadataFilter(
        department_keys=(),
        category_prefixes=(),
        topic_keys=(),
        shortcut_id=None,
    )
    assert metadata_filter.is_empty is True


def test_route_refuses_nonempty_server_filter_before_query(
    public_harness: PublicHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_api,
        "current_public_filter",
        lambda: PublicMetadataFilter(department_keys=("01-科管",)),
    )

    with pytest.raises(
        AssertionError, match="公共查询必须覆盖为空 metadata filter"
    ):
        public_harness.client.post(
            PUBLIC_CHAT_PATH,
            headers=public_harness.headers,
            json={"query": "合成问题"},
        )
