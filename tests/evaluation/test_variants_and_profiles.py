"""P08 tuning-only 选择与主备向量空间隔离。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.v2.models import MetricReport, MetricValue
from evaluation.v2.variants import select_tuning_candidate
from rag_app.composition.profiles import EmbeddingTopologyProfile, load_profile


def _report(split: str) -> MetricReport:
    values = {
        name: MetricValue(value=value, sample_count=8, status="ok")
        for name, value in {
            "recall_at_5": 1.0,
            "refusal_f1": 1.0,
            "citation_validity_rate": 1.0,
            "source_range_coverage": 1.0,
            "negative_leakage_at_10": 0.0,
            "wrong_scope_hit_count": 0,
            "wrong_revision_hit_count": 0,
            "wrong_vector_space_attempt_count": 0,
        }.items()
    }
    return MetricReport(
        lane="offline-structural",
        variant_id="baseline",
        split=split,
        metrics=values,
        categories={},
        engineering={},
    )


def test_parameter_selection_rejects_holdout_metrics() -> None:
    with pytest.raises(ValueError, match="holdout"):
        select_tuning_candidate((_report("holdout"),))


def test_hot_standby_profile_keeps_vector_spaces_distinct() -> None:
    profile = load_profile(
        Path("configs/profiles/dev-jina-qwen37-hot-standby.json")
    )
    topology = profile.components.embedding_topology
    assert isinstance(topology, EmbeddingTopologyProfile)
    assert topology.standby is not None

    assert topology.primary.provider == "jina-embedding"
    assert topology.primary.vector_name == "dense_primary"
    assert topology.standby.provider == "aliyun-qwen37-embedding"
    assert topology.standby.vector_name == "dense_standby"
    assert topology.primary.vector_name != topology.standby.vector_name
