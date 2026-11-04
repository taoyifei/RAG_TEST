"""个人原件预占、队列名额、重启及并发写入的额度契约。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from wanshitong_gateway.library_quota import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    PersonalQuotaStore,
)


def _native(
    identifier: str, size: int, status: str = "completed"
) -> dict[str, Any]:
    return {
        "id": identifier,
        "file_size": size,
        "parse_status": status,
        "knowledge_base_id": "personal",
    }


def test_default_limits_and_override_do_not_include_private_usage(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    assert store.limits("a") == {
        "max_bytes": 250 * 1024 * 1024,
        "max_files": 50,
    }
    assert store.set_limits("a", 1234, 7) == {"max_bytes": 1234, "max_files": 7}
    assert store.limits("a") == {"max_bytes": 1234, "max_files": 7}
    assert store.limits("b") == {
        "max_bytes": DEFAULT_MAX_BYTES,
        "max_files": DEFAULT_MAX_FILES,
    }
    store.set_limits("a", DEFAULT_MAX_BYTES, DEFAULT_MAX_FILES)
    with store._connections.transaction() as connection:
        assert connection.execute(
            "SELECT count(*) FROM library_personal_limits WHERE owner_id='a'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("limits", [(0, 1), (1, 0), (-1, 50), (True, 50)])
def test_invalid_override_does_not_save(
    tmp_path: Path, limits: tuple[int, int]
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    with pytest.raises(ValueError):
        store.set_limits("a", *limits)
    assert store.limits("a")["max_files"] == 50


def test_reservations_count_towards_bytes_and_warn_at_eighty_percent(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    store.set_limits("a", 100, 50)
    first = store.reserve_upload("a", "本人", "one.txt", 80, [])
    summary = store.summary("a", [])
    assert summary["warning"] is True
    assert summary["used_bytes"] == 80
    assert summary["used_files"] == 1
    assert summary["active_parses"] == 1
    store.reserve_upload("a", "本人", "two.txt", 20, [])
    assert store.summary("a", [])["full"] is True
    with pytest.raises(HTTPException) as denied:
        store.reserve_upload("a", "本人", "three.txt", 1, [])
    assert denied.value.status_code == 409
    store.set_state(first.job_id, "failed", error="解析失败")
    assert store.summary("a", [])["used_bytes"] == 100
    store.mark_deleted(first.job_id, "a")
    assert store.summary("a", [])["used_bytes"] == 20
    assert store.summary("a", [])["upload_allowed"] is True


def test_completed_native_mapping_defends_against_stale_snapshots(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    job = store.reserve_upload("a", "本人", "one.txt", 80, [])
    store.accept_native(job.job_id, _native("native-one", 0))
    assert store.summary("a", [])["used_bytes"] == 80
    assert store.summary("a", [_native("native-one", 80)])["used_bytes"] == 80
    assert store.summary("a", [_native("native-one", 0)])["used_files"] == 1
    restarted = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    assert restarted.job(job.job_id) == store.job(job.job_id)
    assert restarted.summary("a", [])["used_bytes"] == 80


def test_file_limit_is_exactly_fifty_even_when_native_snapshot_is_old(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    for index in range(50):
        job = store.reserve_upload("a", "本人", f"{index}.txt", 1, [])
        store.accept_native(job.job_id, _native(f"native-{index}", 1))
    assert store.summary("a", [])["used_files"] == 50
    with pytest.raises(HTTPException) as denied:
        store.reserve_upload("a", "本人", "fifty-one.txt", 1, [])
    assert denied.value.status_code == 409
    assert len(store.jobs("a")) == 50
    assert store.summary("b", [])["used_files"] == 0


def test_six_pending_reservations_are_one_active_and_five_waiting(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    jobs = [
        store.reserve_upload("a", "本人", f"{index}.txt", 1, [])
        for index in range(6)
    ]
    for job in jobs:
        store.set_state(job.job_id, "queued")
    summary = store.summary("a", [])
    assert summary["active_parses"] == 1
    assert summary["queued_parses"] == 5
    with pytest.raises(HTTPException) as denied:
        store.reserve_upload("a", "本人", "seventh.txt", 1, [])
    assert denied.value.status_code == 429
    active = store.claim_next("a", [])
    assert active is not None and active.job_id == jobs[0].job_id
    assert store.claim_next("a", []) is None


def test_native_pending_prevents_dispatch_but_other_owner_is_independent(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    first = store.reserve_upload("a", "本人", "one.txt", 1, [])
    store.set_state(first.job_id, "queued")
    assert store.claim_next("a", [_native("legacy", 1, "processing")]) is None
    other = store.reserve_upload("b", "另一人", "two.txt", 1, [])
    store.set_state(other.job_id, "queued")
    assert store.claim_next("b", []) is not None


def test_parallel_instances_cannot_over_reserve_or_claim_two_slots(
    tmp_path: Path,
) -> None:
    database = tmp_path / "gateway.sqlite3"
    first, second = PersonalQuotaStore(database), PersonalQuotaStore(database)
    first.set_limits("a", 250, 50)

    def reserve(index: int) -> str | None:
        store = first if index % 2 else second
        try:
            return store.reserve_upload(
                "a", "本人", f"{index}.txt", 50, []
            ).job_id
        except HTTPException as error:
            assert error.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = [value for value in executor.map(reserve, range(8)) if value]
    assert len(accepted) == 5
    assert first.summary("a", [])["used_bytes"] == 250
    for identifier in accepted:
        first.set_state(identifier, "queued")
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda store: store.claim_next("a", []), (first, second)
            )
        )
    assert sum(claim is not None for claim in claims) == 1


def test_uncertain_submission_holds_slot_and_does_not_blindly_replay(
    tmp_path: Path,
) -> None:
    clock = [1000.0]
    store = PersonalQuotaStore(
        tmp_path / "gateway.sqlite3", clock=lambda: clock[0]
    )
    first = store.reserve_upload("a", "本人", "one.txt", 1, [])
    second = store.reserve_upload("a", "本人", "two.txt", 1, [])
    store.set_state(first.job_id, "queued")
    store.set_state(second.job_id, "queued")
    claimed = store.claim_next("a", [])
    assert claimed is not None
    store.set_state(first.job_id, "uncertain", error="响应未确认")
    assert store.claim_next("a", []) is None
    current = store.job(first.job_id)
    assert current is not None
    with pytest.raises(HTTPException) as waiting:
        store.reset_retry(current, [])
    assert waiting.value.status_code == 409
    clock[0] = 1400.0
    # 只有本人显式重试才重新排队；后台不会凭时间自动重发。
    store.reset_retry(current, [])
    assert store.job(first.job_id).state == "queued"  # type: ignore[union-attr]


def test_existing_full_quota_can_retry_without_double_counting(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    store.set_limits("a", 10, 1)
    original = _native("native-one", 10)
    assert store.summary("a", [original])["full"] is True
    retry = store.reserve_retry("a", "本人", original, [original])
    summary = store.summary("a", [original])
    assert summary["used_bytes"] == 10 and summary["used_files"] == 1
    assert summary["active_parses"] == 1
    with pytest.raises(HTTPException) as busy:
        store.reserve_retry("a", "本人", original, [original])
    assert busy.value.status_code == 409
    assert store.job(retry.job_id) is not None


def test_reduced_quota_preserves_existing_files_and_does_not_cancel_jobs(
    tmp_path: Path,
) -> None:
    store = PersonalQuotaStore(tmp_path / "gateway.sqlite3")
    job = store.reserve_upload("a", "本人", "one.txt", 10, [])
    store.set_limits("a", 1, 1)
    assert store.summary("a", [])["full"] is True
    assert store.job(job.job_id) is not None
    assert store.summary("a", [])["used_bytes"] == 10
