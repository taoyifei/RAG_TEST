"""阶段 04 湾事通部门影子开关门禁。"""

from __future__ import annotations

import pytest

from rag_app.wanshitong.settings import WanshitongSettings


def test_department_shadow_defaults_off_and_requires_wanshitong_mode() -> None:
    default = WanshitongSettings.from_environment({})
    universal = WanshitongSettings.from_environment(
        {
            "RAG_PRODUCT_MODE": "universal",
            "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED": "true",
        }
    )
    enabled = WanshitongSettings.from_environment(
        {
            "RAG_PRODUCT_MODE": "wanshitong",
            "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED": "true",
        }
    )

    assert default.department_shadow_enabled is False
    assert universal.department_shadow_enabled is False
    assert enabled.department_shadow_enabled is True


def test_department_shadow_rejects_ambiguous_boolean() -> None:
    with pytest.raises(ValueError, match="仅支持 true 或 false"):
        WanshitongSettings.from_environment(
            {
                "RAG_PRODUCT_MODE": "wanshitong",
                "RAG_WANSHITONG_DEPARTMENT_SHADOW_ENABLED": "yes",
            }
        )


def test_natural_public_defaults_off_and_has_fixed_engine() -> None:
    assert (
        WanshitongSettings.from_environment({}).natural_public_enabled is False
    )
    candidate = WanshitongSettings.from_environment(
        {
            "RAG_PRODUCT_MODE": "wanshitong",
            "RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED": "true",
        }
    )
    assert candidate.natural_public_enabled is True
    assert candidate.natural_public_engine == "wk-standard-pc-v1"
    with pytest.raises(ValueError, match="不是受支持的引擎"):
        WanshitongSettings.from_environment(
            {
                "RAG_PRODUCT_MODE": "wanshitong",
                "RAG_WANSHITONG_NATURAL_PUBLIC_ENGINE": "unknown",
            }
        )
