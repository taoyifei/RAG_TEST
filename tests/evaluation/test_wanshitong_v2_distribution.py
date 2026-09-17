"""湾事通 V2 自然问法、对抗类别和延迟抽样分布。"""

from __future__ import annotations

from collections import Counter

from evaluation.wanshitong.v2.validation import (
    ADVERSARIAL_CATEGORIES,
    LATENCY_BUCKETS,
    NATURAL_STYLES,
    validate_dataset,
)


def test_v2_distributions_and_semantic_seed_links() -> None:
    datasets = validate_dataset()["datasets"]
    formal = datasets["formal-54.ndjson"]
    natural = datasets["natural-60.ndjson"]
    adversarial = datasets["adversarial-30.ndjson"]
    latency = datasets["latency-24.ndjson"]

    assert len(formal) == 54
    assert Counter(case["question_style"] for case in natural) == NATURAL_STYLES
    assert sum(len(case["question"]) <= 18 for case in natural) >= 30
    assert Counter(case["adversarial_category"] for case in adversarial) == (
        ADVERSARIAL_CATEGORIES
    )
    assert Counter(case["latency_bucket"] for case in latency) == LATENCY_BUCKETS
    assert all(case["required_atoms"] for case in formal + natural + adversarial)
