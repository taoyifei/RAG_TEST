"""验证结果不能跨连接、参数、模式和生命周期复用。"""

from pathlib import Path

import pytest

from rag_app.product.models import ProviderConnectionDraft
from tests.composition.test_p11_r2_conformance import _draft
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


def test_unrelated_connection_and_changed_policy_do_not_admit_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        credential, _, jina, aliyun = create_provider_connections(harness)
        unrelated = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="不相关合成连接",
                provider_type="jina",
                credential_id=credential,
            )
        )
        validate_five_operations(harness, unrelated.connection_id, aliyun)
        profile = harness.runtime.control.create_profile(
            _draft(kb, jina, aliyun)
        )
        missing = harness.runtime.control.profile_validation_issues(
            profile.profile_revision_id
        )
        assert set(missing) == {
            f"{jina}:embedding.document",
            f"{jina}:embedding.query",
        }
        first = harness.runtime.providers.validate(
            jina,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        assert first.validation_mode == "mock"
        assert first.http_category == "mock_200"
        assert (
            harness.runtime.control.get_connection(jina).status != "validated"
        )
        for operation, model in (
            ("embedding.document", "jina-embeddings-v5-text-small"),
            ("reranking", "jina-reranker-v3.5"),
        ):
            harness.runtime.providers.validate(
                jina,
                operation=operation,
                model=model,
                expected_dimension=1024 if operation != "reranking" else None,
            )
        assert (
            harness.runtime.control.get_connection(jina).status == "validated"
        )
        assert (
            harness.runtime.control.profile_validation_issues(
                profile.profile_revision_id
            )
            == ()
        )
        changed = harness.runtime.control.create_profile(
            _draft(kb, jina, aliyun).model_copy(
                update={
                    "standby_query_policy": {
                        "text_type": "query",
                        "query_instruct": "修改后的检索参数",
                    }
                }
            )
        )
        assert harness.runtime.control.profile_validation_issues(
            changed.profile_revision_id
        ) == (f"{aliyun}:embedding.query",)
        connection = harness.runtime.control.get_connection(jina)
        harness.runtime.control.update_connection(
            jina,
            expected_version=connection.configuration_version,
            changes={"enabled": False},
        )
        assert set(
            harness.runtime.control.profile_validation_issues(
                profile.profile_revision_id
            )
        ) == {f"{jina}:embedding.document", f"{jina}:embedding.query"}
    finally:
        harness.close()


def test_resolved_defaults_are_stable_and_metadata_does_not_reindex(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        draft = _draft(kb, jina, aliyun)
        first = harness.runtime.control.create_profile(draft)
        explicit = draft.model_copy(
            update={
                "primary_document_policy": dict(first.primary_document_policy),
                "primary_query_policy": dict(first.primary_query_policy),
                "standby_document_policy": dict(first.standby_document_policy),
                "standby_query_policy": dict(first.standby_query_policy),
                "retrieval_policy": dict(first.retrieval_policy),
            }
        )
        second = harness.runtime.control.create_profile(explicit)
        assert (
            first.index_semantic_fingerprint
            == second.index_semantic_fingerprint
        )
        assert first.serving_fingerprint == second.serving_fingerprint
        old_service = harness.runtime.profiles._resolve(first)
        connection = harness.runtime.control.get_connection(jina)
        harness.runtime.control.update_connection(
            jina,
            expected_version=connection.configuration_version,
            changes={"display_name": "改正显示名"},
        )
        third = harness.runtime.control.create_profile(explicit)
        assert (
            first.index_semantic_fingerprint == third.index_semantic_fingerprint
        )
        assert harness.runtime.profiles._resolve(first) is not old_service
        # 已分发请求仍能持有旧服务，直至 Runtime 生命周期结束才关闭。
        assert harness.runtime.profiles._resolve(
            first
        ) is harness.runtime.profiles._resolve(first)
        before_budget = harness.runtime.profiles.serving_contract(first)[2]
        connection = harness.runtime.control.get_connection(aliyun)
        harness.runtime.control.update_connection(
            aliyun,
            expected_version=connection.configuration_version,
            changes={"request_budget": 1},
        )
        fourth = harness.runtime.control.create_profile(explicit)
        assert (
            fourth.index_semantic_fingerprint
            == first.index_semantic_fingerprint
        )
        assert fourth.serving_fingerprint != first.serving_fingerprint
        assert (
            harness.runtime.profiles.serving_contract(first)[2] != before_budget
        )
    finally:
        harness.close()
