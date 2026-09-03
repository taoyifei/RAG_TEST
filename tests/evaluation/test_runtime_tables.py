"""P08 空列与 omitted 列的生产 Chunk 复现回归。"""

from __future__ import annotations

from pathlib import Path

from evaluation.v2.dataset import load_dataset_directory
from evaluation.v2.runtime import execute_offline_variant
from evaluation.v2.variants import offline_variants
from rag_app.composition.profiles import load_profile


def test_empty_and_omitted_table_markers_are_reproducible(
    tmp_path: Path,
) -> None:
    dataset = load_dataset_directory(Path("evaluation/datasets/synthetic"))
    selected_ids = {"eval_table_empty_middle", "eval_table_omitted"}
    cases = tuple(
        case for case in dataset.cases if case.case_id in selected_ids
    )
    execution = execute_offline_variant(
        dataset,
        cases,
        offline_variants()[7],
        requested_profile=load_profile(
            Path("configs/profiles/dev-offline.json")
        ),
        data_directory=tmp_path / "runtime",
    )

    assert {item.case_id for item in execution.observations} == selected_ids
    assert all(item.citation_valid for item in execution.observations)
    assert all(
        item.matched_source_range_count == 1
        for item in execution.observations
    )
    assert execution.errors == ()
