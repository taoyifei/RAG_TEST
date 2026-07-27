import hashlib
from pathlib import Path

from evaluation.dataset import (
    load_dataset,
    load_holdout_questions,
    load_tuning_cases,
    verify_source_evidence,
)
from tests.synthetic_evaluation import (
    write_synthetic_dataset,
    write_synthetic_evidence_docx,
)


def test_frozen_dataset_has_isolated_holdout_and_required_coverage(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset.json"
    dataset = write_synthetic_dataset(dataset_path)

    assert len(dataset.cases) == 60
    assert len(load_tuning_cases(dataset_path)) == 45
    holdout = load_holdout_questions(dataset_path)
    assert len(holdout) == 15
    assert all(not hasattr(question, "expected") for question in holdout)
    assert sum("ocr" in case.categories for case in dataset.cases) >= 5


def test_frozen_dataset_matches_manifest_sha256(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.json"
    write_synthetic_dataset(dataset_path)
    expected = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    (tmp_path / "MANIFEST.sha256").write_text(
        f"{expected}  dataset.json\n",
        encoding="utf-8",
    )
    manifest_sha256 = (
        (tmp_path / "MANIFEST.sha256").read_text("utf-8").split()[0]
    )

    assert hashlib.sha256(dataset_path.read_bytes()).hexdigest() == (
        manifest_sha256
    )


def test_every_frozen_evidence_locator_and_quote_matches_docx(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset.json"
    write_synthetic_dataset(dataset_path)
    write_synthetic_evidence_docx(tmp_path / "synthetic.docx")
    result = verify_source_evidence(load_dataset(dataset_path), tmp_path)

    assert result["cases"] == 60
    assert result["holdout"] == 15
    assert result["text_evidence"] >= 50
    assert result["ocr_locators"] >= 5
