"""P11-R2 API 拒绝未知、冲突和不支持的请求策略。"""

from pathlib import Path

import pytest

from tests.api.test_retrieval_profiles import _profile_payload
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)


@pytest.mark.parametrize(
    "changes",
    [
        {
            "primary_query_policy": {
                "task": "retrieval.query",
                "instruction": "不能使用",
            }
        },
        {
            "primary_query_policy": {
                "task": "retrieval.query",
                "query_instruct": "不能使用",
            }
        },
        {
            "primary_query_policy": {
                "task": "retrieval.query",
                "normalize": True,
            }
        },
        {
            "primary_query_policy": {
                "task": "retrieval.query",
                "normalized": False,
            }
        },
        {
            "primary_query_policy": {
                "task": "retrieval.query",
                "normalized": "true",
            }
        },
        {
            "standby_query_policy": {
                "text_type": "query",
                "instruction": "请显式迁移",
            }
        },
        {"standby_query_policy": {"text_type": "query", "unknown": 42}},
        {"evidence_policy": {"unsupported_score": 0.8}},
        {
            "retrieval_policy": {"minimum_support_items": 2},
            "evidence_policy": {"minimum_units": 1},
        },
        {
            "retrieval_policy": {
                "dense_semantic_calibration_state": "LIVE_CALIBRATED"
            }
        },
    ],
)
def test_unsupported_policy_is_422(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        response = harness.client.post(
            f"/api/v1/knowledge-bases/{kb}/retrieval-profiles",
            headers=harness.write_headers,
            json={**_profile_payload(jina, aliyun), **changes},
        )
        assert response.status_code == 422
        assert harness.runtime.control.list_profiles(kb) == ()
    finally:
        harness.close()
