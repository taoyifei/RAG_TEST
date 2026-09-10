"""本机历史的失败可见性、加密、权限和重启回归。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import pytest

from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.errors import IndexNotReady, ProviderUnavailable
from rag_app.core.identifiers import new_id
from rag_app.core.models import KnowledgeBaseScope
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _upload(harness: ProductHarness, project: str, kb: str) -> str:
    job = harness.runtime.sdk.create_document(
        project,
        kb,
        display_name="公共设备手册.docx",
        content=build_package(
            "<w:p><w:r><w:t>设备 MX-41 的维护周期为 14 天。</w:t></w:r></w:p>"
        ),
        media_type=_MEDIA_TYPE,
        idempotency_key="history-fixture",
    )
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            assert current.state.value == "succeeded", current
            assert current.document_id
            return current.document_id
        sleep(0.01)
    raise AssertionError("公开 Fixture 上传超时")


def test_history_records_pre_snapshot_failure_and_survives_restart(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    project, kb = create_project_and_knowledge_base(harness)
    settings = harness.runtime.settings
    response = harness.client.post(
        f"/api/v1/projects/{project}/knowledge-bases/{kb}:answer",
        json={"query": "还没有上传资料的设备周期是多少"},
        headers=harness.write_headers,
    )
    assert response.status_code == 409
    trace_id = response.json()["error"]["trace_id"]
    assert response.headers["X-Trace-Id"] == trace_id
    page = harness.client.get("/api/v1/history").json()
    assert page["total"] == 1
    assert page["items"][0]["status"] == "FAILED"
    assert page["items"][0]["question"] == "还没有上传资料的设备周期是多少"
    assert all(
        item["call_count"] == 0 for item in page["items"][0]["provider_usage"]
    )
    interrupted_id = new_id("trace")
    harness.runtime.history.start(
        interrupted_id,
        KnowledgeBaseScope(project_id=project, knowledge_base_id=kb),
        "模拟进程被终止的公开问题",
        owner_id="local-admin",
        save_body=True,
        conversation_context_digest="d" * 64,
    )
    harness.close()
    with build_product_runtime(settings) as runtime:
        detail = runtime.history.detail(trace_id)
        assert detail["question"] == "还没有上传资料的设备周期是多少"
        assert detail["status"] == "FAILED"
        assert runtime.history.detail(interrupted_id)["status"] == "INTERRUPTED"
        assert (
            runtime.history.detail(interrupted_id)[
                "conversation_context_digest"
            ]
            == "d" * 64
        )
        assert runtime.history.list_history(keyword="没有上传")["total"] == 1
    assert (
        "还没有上传资料".encode()
        not in (settings.data_dir / "universal-rag.sqlite3").read_bytes()
    )


def test_history_retains_cache_diagnostics_and_revokes_deleted_source(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        document_id = _upload(harness, project, kb)
        endpoint = f"/api/v1/projects/{project}/knowledge-bases/{kb}:answer"
        responses = [
            harness.client.post(
                endpoint,
                json={"query": "MX-41"},
                headers=harness.write_headers,
            )
            for _ in range(2)
        ]
        assert all(response.status_code == 200 for response in responses)
        result = responses[-1].json()
        assert result["cache_hit"] is True
        assert responses[-1].headers["X-Trace-Id"] == result["trace_id"]
        detail = harness.client.get(
            "/api/v1/history/" + result["trace_id"]
        ).json()
        assert detail["question"] == "MX-41"
        assert detail["events"]
        assert detail["diagnostics"]["cache_hit"] is True
        assert all(
            item["reason_code"] == "CACHE_HIT"
            for item in detail["provider_usage"]
        )
        response = harness.client.delete(
            f"/api/v1/projects/{project}/knowledge-bases/{kb}/documents/{document_id}",
            headers=harness.write_headers,
        )
        assert response.status_code in {200, 202, 204}
        detail = harness.client.get(
            "/api/v1/history/" + result["trace_id"]
        ).json()
        assert detail["body_available"] is False
        assert detail["question"] is None
        assert detail["answer"] is None
        assert "result" not in detail
        harness.client.cookies.clear()
        assert harness.client.get("/api/v1/history").status_code == 401
    finally:
        harness.close()


def test_metadata_only_and_persistence_failure_are_explicit(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        endpoint = f"/api/v1/projects/{project}/knowledge-bases/{kb}:answer"
        response = harness.client.post(
            endpoint,
            json={"query": "公开问题", "history_mode": "metadata_only"},
            headers=harness.write_headers,
        )
        trace_id = response.json()["error"]["trace_id"]
        item = harness.runtime.history.detail(trace_id)
        assert item["body_message"] == "本次未保存正文"
        assert item["question"] is None
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute("DROP TABLE query_history")
        response = harness.client.post(
            endpoint,
            json={"query": "公开问题"},
            headers=harness.write_headers,
        )
        assert response.status_code == 503
        assert (
            response.json()["error"]["code"] == "TRACE_PERSISTENCE_UNAVAILABLE"
        )
    finally:
        harness.close()


def test_history_global_body_disabled_and_secret_text_redacted(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    project, kb = create_project_and_knowledge_base(harness)
    trace_id = new_id("trace")
    harness.runtime.history.start(
        trace_id,
        KnowledgeBaseScope(project_id=project, knowledge_base_id=kb),
        "Authorization: Bearer test-only",
        owner_id="local-admin",
        save_body=True,
    )
    assert harness.runtime.history.detail(trace_id)["question"] == "[REDACTED]"
    settings = replace(harness.runtime.settings, history_save_body=False)
    harness.close()
    with build_product_runtime(settings) as runtime:
        with pytest.raises(IndexNotReady):
            runtime.sdk.search(
                project, kb, "另一个公开问题", history_mode="full"
            )
        page = runtime.history.list_history(owner_id="sdk")
        assert page["total"] == 1
        assert page["items"][0]["body_saved"] is False


def test_history_first_page_only_decrypts_current_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        _insert_history_metadata(harness, project, kb, count=10_000)
        decryptions = 0
        original_decode = harness.runtime.history._decode

        def _count_decode(row: object) -> dict[str, object]:
            nonlocal decryptions
            decryptions += 1
            return original_decode(row)  # type: ignore[arg-type]

        monkeypatch.setattr(harness.runtime.history, "_decode", _count_decode)
        page = harness.runtime.history.list_history(page_size=20)

        assert page["total"] == 10_000
        assert page["total_is_exact"] is True
        assert len(page["items"]) == 20
        assert decryptions == 20
    finally:
        harness.close()


def test_history_keyword_scan_is_bounded_and_reports_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        _insert_history_metadata(harness, project, kb, count=1_500)
        decryptions = 0

        def _decode(_row: object) -> dict[str, object]:
            nonlocal decryptions
            decryptions += 1
            return {"question": "有界关键词"}

        monkeypatch.setattr(harness.runtime.history, "_decode", _decode)
        page = harness.runtime.history.list_history(
            keyword="关键词", page_size=20
        )

        assert page["search_complete"] is False
        assert page["total_is_exact"] is False
        assert page["truncation_reason"] in {"CANDIDATE_LIMIT", "TIME_LIMIT"}
        assert 0 < int(page["scanned_count"]) <= 1_000
        assert decryptions == page["scanned_count"]
        assert len(page["items"]) == 20
    finally:
        harness.close()


def test_history_group_commit_batches_and_isolates_concurrent_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发 STARTED/终态共享 commit，重复 ID 不回滚同批其他请求。"""
    harness = build_product_harness(tmp_path)
    history = harness.runtime.history
    project, kb = create_project_and_knowledge_base(harness)
    scope = KnowledgeBaseScope(project_id=project, knowledge_base_id=kb)
    batch_sizes: list[int] = []
    original_execute = history._execute_write_batch

    def observe_batch(commands: object) -> None:
        batch_sizes.append(len(commands))  # type: ignore[arg-type]
        original_execute(commands)  # type: ignore[arg-type]

    monkeypatch.setattr(history, "_execute_write_batch", observe_batch)
    trace_ids = tuple(new_id("trace") for _ in range(16))

    def start_one(trace_id: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        history.start(
            trace_id,
            scope,
            "并发批量历史问题",
            owner_id="local-admin",
            save_body=False,
        )

    def finish_one(trace_id: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        history.finish(
            trace_id,
            result=None,
            error=None,
            cancelled=True,
        )

    try:
        start_barrier = threading.Barrier(len(trace_ids) + 1)
        with ThreadPoolExecutor(max_workers=len(trace_ids)) as executor:
            starts = tuple(
                executor.submit(start_one, trace_id, start_barrier)
                for trace_id in trace_ids
            )
            start_barrier.wait()
            for future in starts:
                future.result()

        finish_barrier = threading.Barrier(len(trace_ids) + 1)
        with ThreadPoolExecutor(max_workers=len(trace_ids)) as executor:
            finishes = tuple(
                executor.submit(finish_one, trace_id, finish_barrier)
                for trace_id in trace_ids
            )
            finish_barrier.wait()
            for future in finishes:
                future.result()

        assert max(batch_sizes) > 1
        assert all(
            history.detail(trace_id)["status"] == "CANCELLED"
            for trace_id in trace_ids
        )

        peer_trace_id = new_id("trace")
        isolation_barrier = threading.Barrier(3)
        before_isolation = len(batch_sizes)
        with ThreadPoolExecutor(max_workers=2) as executor:
            duplicate = executor.submit(
                start_one,
                trace_ids[0],
                isolation_barrier,
            )
            peer = executor.submit(
                start_one,
                peer_trace_id,
                isolation_barrier,
            )
            isolation_barrier.wait()
            with pytest.raises(ProviderUnavailable):
                duplicate.result()
            peer.result()
        assert any(size > 1 for size in batch_sizes[before_isolation:])
        assert history.detail(peer_trace_id)["status"] == "STARTED"
        assert history.detail(trace_ids[0])["status"] == "CANCELLED"
    finally:
        harness.close()

    assert history._writer is not None
    assert history._writer.is_alive() is False


def _insert_history_metadata(
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
    *,
    count: int,
) -> None:
    """快速写入不含正文的合成元数据，专用于分页复杂度回归。"""
    now = datetime.now(UTC)
    expires_at = (now + timedelta(days=1)).isoformat()
    rows = [
        (
            f"trace_{index:032x}",
            project_id,
            knowledge_base_id,
            "local-admin",
            "pagination-test",
            1,
            (now - timedelta(microseconds=index)).isoformat(),
            (now - timedelta(microseconds=index)).isoformat(),
            expires_at,
            "ANSWERED",
            "0" * 64,
            0,
            None,
            None,
            1,
            "{}",
        )
        for index in range(count)
    ]
    with harness.runtime.connections.transaction(write=True) as connection:
        connection.executemany(
            "INSERT INTO query_history("
            "trace_id, project_id, knowledge_base_id, owner_id, instance_id, "
            "process_id, created_at, finished_at, expires_at, status, "
            "question_sha256, body_saved, ciphertext, nonce, duration_ms, "
            "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            rows,
        )
