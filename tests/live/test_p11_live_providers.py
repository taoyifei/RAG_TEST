"""现有 release acceptance 入口的可选择真实阶段测试组。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rag_app.product.live_acceptance import STEPS, run_acceptance


@pytest.mark.live_provider
@pytest.mark.parametrize("step", STEPS)
def test_page_managed_live_stage(step: str) -> None:
    """只读取非 Secret 配置与既有页面连接，预算跨 pytest 运行保留。"""
    config_path = os.environ.get("P11_ACCEPTANCE_CONFIG")
    config = (
        None
        if not config_path
        else json.loads(Path(config_path).read_text(encoding="utf-8"))
    )
    report = run_acceptance(
        config,
        steps=(step,),
        resume=True,
        live=(
            os.environ.get("P11_LIVE_AUTHORIZED") == "true"
            and os.environ.get("RAG_TEST_NETWORK") == "live"
        ),
    )
    result = report["steps"][step]
    print("P11_LIVE_ACCEPTANCE=" + json.dumps(report, ensure_ascii=False))
    assert result["status"] == "PASS", result["reason"]
