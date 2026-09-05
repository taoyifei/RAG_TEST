"""质量证据独立持久化，Mock 与连接成功不能晋级生产校准。"""

from pathlib import Path

import pytest

from rag_app.product.quality import QualityValidationRecord
from tests.product_support import (
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


def test_quality_record_binding_and_mock_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        jina_credential, _, jina, aliyun = create_provider_connections(harness)
        validate_five_operations(harness, jina, aliyun)
        profile_id = activate_hot_standby_profile(harness, kb, jina, aliyun)
        profile = harness.runtime.control.get_profile(profile_id)
        quality = harness.runtime.control.quality
        assert quality.states(profile_id) == {}
        assert quality.calibrated_spaces(profile_id) == ()
        record = QualityValidationRecord(
            profile_revision_id=profile_id,
            kind="retrieval_quality_verified",
            validation_mode="mock",
            run_id="TEST_ONLY_synthetic_record",
            dataset_sha256="a" * 64,
            artifact_sha256="b" * 64,
            index_fingerprint=profile.index_semantic_fingerprint,
            serving_fingerprint=profile.serving_fingerprint,
            gates={
                "independent_labels": True,
                "source_precision": True,
                "recall": True,
                "negative_leakage": True,
            },
            independent_holdout=True,
            labeled_queries=20,
            negative_queries=10,
            citation_source_precision=0.95,
            recall=0.9,
            negative_leakage=0,
        )
        quality.record(record)
        assert quality.calibrated_spaces(profile_id) == ()
        # 合成记录只检验状态机；没有运行真实质量评估。
        quality.record(record.model_copy(update={"validation_mode": "live"}))
        assert len(quality.calibrated_spaces(profile_id)) == 2
        assert (
            harness.runtime.sdk.health().remote_production_profile_ready
            is False
        )
        assert (
            harness.runtime.sdk.health().remote_dense_confidence_calibrated
            is False
        )
        resolved = harness.runtime.profiles._resolve(profile)
        assert resolved.retrieval._policy.dense_semantic_enabled is False
        quality.record(
            record.model_copy(
                update={
                    "kind": "offline_evaluation_ready",
                    "validation_mode": "offline",
                    "gates": {"offline_eval": True},
                }
            )
        )
        assert harness.runtime.sdk.health().offline_evaluation_v3_ready is True
        with harness.runtime.connections.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM quality_validation_records"
                ).fetchone()[0]
                == 3
            )
        harness.runtime.credentials.rotate(jina_credential, "synthetic-rotated")
        assert quality.states(profile_id) == {}
        assert quality.calibrated_spaces(profile_id) == ()
        assert harness.runtime.profiles._resolve(profile) is not resolved
    finally:
        harness.close()
