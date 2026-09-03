"""P08 不可变产物、Manifest 安全与 Live 守卫。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from evaluation.v2.artifacts import (
    create_run_directory,
    validate_manifest_safety,
    write_json,
)
from evaluation.v2.runner import (
    LiveLaneBlockedError,
    RunOptions,
    run_evaluation,
)


class _UnsafeManifest(BaseModel):
    query: str


class _UnsafePathManifest(BaseModel):
    artifact: str


def _options(tmp_path: Path, lane: str, **updates: object) -> RunOptions:
    values: dict[str, object] = {
        "dataset": Path("evaluation/datasets/synthetic"),
        "profile": Path("configs/profiles/dev-offline.json"),
        "lane": lane,
        "reports_root": tmp_path,
        "gates": Path("evaluation/gates/p08-gates.json"),
    }
    values.update(updates)
    return RunOptions(**values)  # type: ignore[arg-type]


def test_run_directory_and_files_cannot_be_overwritten(tmp_path: Path) -> None:
    run = create_run_directory(tmp_path, "p08-20260903T000000Z-deadbeef")
    write_json(run / "result.json", {"status": "ok"})

    with pytest.raises(FileExistsError):
        create_run_directory(tmp_path, run.name)
    with pytest.raises(FileExistsError):
        write_json(run / "result.json", {"status": "changed"})


@pytest.mark.parametrize(
    "manifest",
    (
        _UnsafeManifest(query="private body"),
        _UnsafePathManifest(artifact="/private/document.docx"),
    ),
)
def test_manifest_rejects_query_body_and_absolute_paths(
    manifest: BaseModel,
) -> None:
    with pytest.raises(ValueError, match="Manifest"):
        validate_manifest_safety(manifest)


def test_live_lane_requires_explicit_authorization(tmp_path: Path) -> None:
    with pytest.raises(
        LiveLaneBlockedError,
        match="BLOCKED_MISSING_EXPLICIT_EGRESS_ACKNOWLEDGEMENT",
    ):
        run_evaluation(_options(tmp_path, "live-primary"))


def test_live_lane_with_flags_still_requires_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    with pytest.raises(LiveLaneBlockedError, match="BLOCKED_NO_CREDENTIALS"):
        run_evaluation(
            _options(
                tmp_path,
                "live-primary",
                live_provider=True,
                acknowledge_egress=True,
                budget_requests=1,
                budget_tokens=1,
            )
        )


def test_offline_lane_rejects_live_flags(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="offline-structural"):
        run_evaluation(
            _options(
                tmp_path,
                "offline-structural",
                live_provider=True,
            )
        )
