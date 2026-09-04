"""P11 候选版单机性能与容量观察证据。"""

from __future__ import annotations

import json
import os
import resource
from math import ceil
from pathlib import Path
from time import monotonic, sleep

import pytest
from qdrant_client import QdrantClient

from rag_app.core.models import Job
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)

pytestmark = pytest.mark.local_integration

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_SAMPLE_COUNT = 20


def _required_environment(name: str) -> str:
    """读取本地集成验收的必需变量。

    Args:
        name: 环境变量名。

    Returns:
        非空变量值。

    """
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"需要显式配置 {name}")
    return value


def _wait(harness: ProductHarness, job: Job) -> Job:
    """等待单文档索引任务结束。

    Args:
        harness: Product 测试资源。
        job: 已排队任务。

    Returns:
        结束状态任务。

    """
    deadline = monotonic() + 30
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.02)
    raise AssertionError("P11 性能观察索引超时。")


def _percentile(values: list[float], quantile: float) -> float:
    """返回最近秩百分位。

    Args:
        values: 非空耗时列表。
        quantile: 0—1 之间的百分位。

    Returns:
        最近秩样本值。

    """
    ordered = sorted(values)
    return ordered[max(0, ceil(len(ordered) * quantile) - 1)]


def _latency_summary(values: list[float]) -> dict[str, float]:
    """生成毫秒 p50/p95。

    Args:
        values: 秒单位耗时列表。

    Returns:
        毫秒统计。

    """
    return {
        "p50_ms": round(_percentile(values, 0.50) * 1000, 3),
        "p95_ms": round(_percentile(values, 0.95) * 1000, 3),
    }


def test_candidate_records_single_host_performance_without_sla_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用真实 Qdrant 与 Mock Provider 记录 P11 单机观察值。

    Args:
        tmp_path: pytest 隔离目录。
        monkeypatch: 环境托管测试凭据注入器。

    Returns:
        无返回值。

    """
    qdrant_url = _required_environment("RAG_TEST_QDRANT_SOURCE_URL")
    key_file = Path(_required_environment("RAG_TEST_QDRANT_SOURCE_KEY_FILE"))
    output = Path(_required_environment("RAG_P11_PERFORMANCE_OUTPUT"))
    image_size = int(_required_environment("RAG_P11_IMAGE_SIZE_BYTES"))
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(
        tmp_path,
        qdrant_url=qdrant_url,
        qdrant_api_key_file=key_file,
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection, aliyun_connection = create_provider_connections(
            harness
        )
        validate_five_operations(harness, jina_connection, aliyun_connection)
        activate_hot_standby_profile(
            harness,
            knowledge_base_id,
            jina_connection,
            aliyun_connection,
        )
        content = build_package(
            "<w:p><w:r><w:t>公开合成性能文本：采购申请审批后归档。</w:t></w:r></w:p>"
        )
        started = monotonic()
        job = _wait(
            harness,
            harness.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="P11-公开合成性能.docx",
                content=content,
                media_type=_MEDIA_TYPE,
                idempotency_key="p11-performance-document",
            ),
        )
        index_seconds = monotonic() - started
        assert job.state.value == "succeeded"

        search_latencies: list[float] = []
        answer_latencies: list[float] = []
        for index in range(_SAMPLE_COUNT):
            started = monotonic()
            harness.runtime.sdk.search(
                project_id,
                knowledge_base_id,
                f"采购申请归档检索 {index}",
                limit=5,
            )
            search_latencies.append(monotonic() - started)
        for index in range(_SAMPLE_COUNT):
            started = monotonic()
            harness.runtime.sdk.answer(
                project_id,
                knowledge_base_id,
                f"采购申请如何归档 {index}",
                limit=5,
            )
            answer_latencies.append(monotonic() - started)
        first = harness.runtime.sdk.search(
            project_id, knowledge_base_id, "缓存观察", limit=5
        )
        second = harness.runtime.sdk.search(
            project_id, knowledge_base_id, "缓存观察", limit=5
        )

        sqlite_latencies: list[float] = []
        for _ in range(_SAMPLE_COUNT):
            started = monotonic()
            with harness.runtime.connections.transaction() as connection:
                chunk_count = int(
                    connection.execute(
                        "SELECT count(*) FROM chunks WHERE revision_id=?",
                        (job.revision_id,),
                    ).fetchone()[0]
                )
            sqlite_latencies.append(monotonic() - started)
        qdrant = QdrantClient(
            url=qdrant_url,
            api_key=key_file.read_text(encoding="utf-8").strip(),
            check_compatibility=False,
        )
        qdrant_latencies: list[float] = []
        try:
            for _ in range(_SAMPLE_COUNT):
                started = monotonic()
                qdrant.get_collection(job.revision_id)
                qdrant_latencies.append(monotonic() - started)
        finally:
            qdrant.close()

        with harness.runtime.connections.transaction() as connection:
            provider_rows = tuple(
                connection.execute(
                    "SELECT operation, selected_slot, count(*) "
                    "FROM provider_operation_events "
                    "GROUP BY operation, selected_slot "
                    "ORDER BY operation, selected_slot"
                ).fetchall()
            )
        report = {
            "answer": _latency_summary(answer_latencies),
            "cache": {
                "first_hit": bool(first.cache_hit),
                "second_hit": bool(second.cache_hit),
            },
            "chunk_count": chunk_count,
            "chunk_per_second": round(chunk_count / index_seconds, 3),
            "container_image_size_bytes": image_size,
            "index_seconds": round(index_seconds, 3),
            "memory_peak_kib": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
            "notes": {
                "answer_path": "当前 Product answer 与 search 共用检索回答链",
                "provider_mode": "httpx.MockTransport",
                "qdrant_mode": "qdrant-server",
                "sla": False,
            },
            "provider_calls": [
                {
                    "count": int(row[2]),
                    "operation": str(row[0]),
                    "selected_slot": row[1],
                }
                for row in provider_rows
            ],
            "qdrant_get_collection": _latency_summary(qdrant_latencies),
            "sample_count": _SAMPLE_COUNT,
            "search": _latency_summary(search_latencies),
            "sqlite_count": _latency_summary(sqlite_latencies),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        assert second.cache_hit is True
    finally:
        harness.close()
