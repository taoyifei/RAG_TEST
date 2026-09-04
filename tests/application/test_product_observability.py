"""P11 Provider 用量与持久预算回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.errors import PolicyDenied
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.product_support import (
    build_product_harness,
    create_provider_connections,
)


def test_standby_daily_budget_survives_runtime_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """备用预算不得因 Product Runtime 重启而清零。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    _, _, _, aliyun_connection = create_provider_connections(harness)
    settings = harness.runtime.settings
    harness.runtime.control.reserve_daily_provider_budget(
        aliyun_connection,
        "embedding",
        8,
        request_limit=1,
        token_limit=16,
    )
    harness.close()

    runtime = build_product_runtime(
        settings,
        transport_factory=build_offline_mock_transport,
    )
    try:
        with pytest.raises(PolicyDenied, match="预算已耗尽"):
            runtime.control.reserve_daily_provider_budget(
                aliyun_connection,
                "embedding",
                1,
                request_limit=1,
                token_limit=16,
            )
    finally:
        runtime.close()
