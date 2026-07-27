from pathlib import Path

import pytest

from rag_app.index.planner import (
    DiscoveredSource,
    plan_incremental_sync,
)
from rag_app.state import JobKind, StateStore
from rag_app.state.plans import SyncItemState, SyncPlanStore

_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64


def _job(tmp_path: Path) -> tuple[StateStore, str]:
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    job = state.create_job(
        idempotency_key="incremental:plan",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    return state, job.job_id


def test_plan_is_idempotently_persisted_and_running_item_is_reclaimed(
    tmp_path: Path,
) -> None:
    state, job_id = _job(tmp_path)
    plan = plan_incremental_sync(
        (
            DiscoveredSource("甲.docx", "a" * 64),
            DiscoveredSource("乙.docx", "b" * 64),
        ),
        state.list_active_sources(),
    )
    plans = SyncPlanStore(state.path)
    plans.initialize()

    plans.save(job_id, plan)
    plans.save(job_id, plan)
    assert len(plans.list_items(job_id)) == 2

    first = plans.claim_next(job_id)
    assert first is not None
    assert first.state == SyncItemState.RUNNING
    assert first.attempt == 1

    reopened = SyncPlanStore(state.path)
    reopened.initialize()
    reclaimed = reopened.claim_next(job_id)
    assert reclaimed is not None
    assert reclaimed.item_id == first.item_id
    assert reclaimed.attempt == 2

    reopened.succeed(reclaimed.item_id)
    second = reopened.claim_next(job_id)
    assert second is not None
    reopened.succeed(second.item_id)
    assert reopened.claim_next(job_id) is None
    assert all(
        item.state == SyncItemState.SUCCEEDED
        for item in reopened.list_items(job_id)
    )


def test_same_job_rejects_changed_plan(tmp_path: Path) -> None:
    state, job_id = _job(tmp_path)
    plans = SyncPlanStore(state.path)
    plans.initialize()
    original = plan_incremental_sync(
        (DiscoveredSource("甲.docx", "a" * 64),),
        (),
    )
    changed = plan_incremental_sync(
        (DiscoveredSource("乙.docx", "b" * 64),),
        (),
    )
    plans.save(job_id, original)

    with pytest.raises(ValueError, match="digest"):
        plans.save(job_id, changed)
