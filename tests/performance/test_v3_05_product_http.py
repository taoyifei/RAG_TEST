"""V3-05 默认 Product HTTP 性能、Trace 与资源门禁。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import resource
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

import httpx
from fastapi.testclient import TestClient

from rag_app.adapters.stores.memory_retrieval_cache import (
    RetrievalCacheMetrics,
)
from rag_app.api.product import create_product_app
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import RagError
from rag_app.core.events import TraceEvent
from rag_app.core.models import KnowledgeBaseScope, SearchAnswerResult
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.search import RetrievalDiagnostics
from rag_app.product.query_history import ProductQueryHistory
from rag_app.product.singleflight import SingleflightMetrics
from rag_app.tracing.models import TraceMode
from rag_app.tracing.store import TraceNotFoundError
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_ROOT = Path(__file__).resolve().parents[2]
_THRESHOLDS = _ROOT / "configs/performance/v3-05-product-http.json"
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_CORPUS_MARKER = "V305-PERFORMANCE-ALPHA-739"
_ISOLATED_CHILD_ENV = "RAG_V305_PERFORMANCE_ISOLATED_CHILD"


@dataclass(frozen=True, slots=True)
class _Sample:
    """一次不含问题、答案或引用正文的 HTTP 测量。"""

    elapsed_ms: float
    header_ms: float
    http_status: int
    trace_id: str | None
    result_signature: str | None
    result_origin: str | None
    singleflight_role: str | None
    singleflight_wait_ms: float
    cache_hit: bool
    provider_calls: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _ModeContext:
    """一个捕获模式的隔离 Runtime、范围与活动 Revision。"""

    mode: str
    harness: ProductHarness
    project_id: str
    knowledge_base_id: str
    revision_id: str


@dataclass(frozen=True, slots=True)
class _WaveOutcome:
    """一个独立测量 wave 及其中顺序执行的并发 batch。"""

    samples: tuple[_Sample, ...]
    batch_origins: tuple[dict[str, int], ...]
    cache_before: RetrievalCacheMetrics
    cache_after: RetrievalCacheMetrics
    singleflight_before: SingleflightMetrics
    singleflight_after: SingleflightMetrics


@dataclass(slots=True)
class _CellAccumulator:
    """跨相邻 A/B 波次聚合一个矩阵 cell 的安全数值样本。"""

    samples: list[_Sample] = field(default_factory=list)
    wave_latencies: list[dict[str, float]] = field(default_factory=list)
    wave_origins: list[dict[str, int]] = field(default_factory=list)
    batch_origins: list[dict[str, int]] = field(default_factory=list)
    cache_before: RetrievalCacheMetrics | None = None
    cache_after: RetrievalCacheMetrics | None = None
    singleflight_before: SingleflightMetrics | None = None
    singleflight_after: SingleflightMetrics | None = None

    def add(
        self,
        outcome: _WaveOutcome,
    ) -> None:
        """追加一个已排空后台 writer 的 HTTP 波次。"""
        if self.cache_before is None:
            self.cache_before = outcome.cache_before
            self.singleflight_before = outcome.singleflight_before
        self.cache_after = outcome.cache_after
        self.singleflight_after = outcome.singleflight_after
        self.samples.extend(outcome.samples)
        self.batch_origins.extend(outcome.batch_origins)
        successful_latencies = [
            sample.elapsed_ms
            for sample in outcome.samples
            if sample.http_status == 200
        ]
        if successful_latencies:
            self.wave_latencies.append(
                {
                    "p50_ms": round(
                        _percentile(successful_latencies, 0.5),
                        3,
                    ),
                    "p95_ms": round(
                        _percentile(successful_latencies, 0.95),
                        3,
                    ),
                }
            )
        self.wave_origins.append(
            dict(
                sorted(
                    Counter(
                        sample.result_origin or "unknown"
                        for sample in outcome.samples
                        if sample.http_status == 200
                    ).items()
                )
            )
        )


class _HistoryOnlyNullTrace:
    """仅供性能基线使用：保留 History，丢弃 Operational Trace。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.TRACE_SINK,
        name="history-only-null-trace",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
    )

    def __init__(self, history: ProductQueryHistory) -> None:
        """绑定仍需完成 query 生命周期的 canonical History。"""
        self._history = history

    def prepare(self, trace_id: str, mode: TraceMode) -> None:
        """接受 SAFE 基线请求但不分配 Operational Trace。"""
        del trace_id, mode

    def start(  # noqa: PLR0913
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
        conversation_context_digest: str | None = None,
    ) -> None:
        """仍建立真实 History STARTED 记录。"""
        self._history.start(
            trace_id,
            scope,
            question,
            owner_id=owner_id,
            save_body=save_body,
            conversation_context_digest=conversation_context_digest,
        )

    def finish(
        self,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        cancelled_calls: tuple[ProviderCall, ...] = (),
    ) -> None:
        """仍按原合同结算真实 History 终态。"""
        self._history.finish(
            trace_id,
            result=result,
            error=error,
            cancelled=cancelled,
            cancelled_calls=cancelled_calls,
        )

    def diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """从 History 返回与启用 Trace 时同源的安全诊断。"""
        return self._history.diagnostics(trace_id)

    def record(self, event: TraceEvent) -> None:
        """基线明确丢弃阶段事件，不隐式建立第二套 Trace。"""
        del event

    def close(self) -> None:
        """适配器不拥有 canonical History。"""


def _load_thresholds() -> dict[str, object]:
    """加载并严格校验测量前冻结的非劣门槛。"""
    payload = json.loads(_THRESHOLDS.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("V3-05 性能门槛必须是 JSON object。")
    if payload.get("schema_version") != ("v3-05-product-http-thresholds-v4"):
        raise AssertionError("V3-05 性能门槛版本错误。")
    if payload.get("capture_modes") != [
        "NONE",
        "SAFE",
        "DIAGNOSTIC",
        "FULL",
    ]:
        raise AssertionError("V3-05 捕获矩阵被修改。")
    if payload.get("concurrencies") != [1, 8, 16]:
        raise AssertionError("V3-05 并发矩阵被修改。")
    if payload.get("minimum_samples_per_cell") != 32:
        raise AssertionError("V3-05 每格最小样本数被修改。")
    if payload.get("minimum_waves_per_cell") != 5:
        raise AssertionError("V3-05 每格最小波次数被修改。")
    if payload.get("minimum_comparison_waves_per_cell") != 40:
        raise AssertionError("V3-05 NONE/SAFE 比较波次数被修改。")
    if payload.get("minimum_comparison_samples_per_cell") != 320:
        raise AssertionError("V3-05 NONE/SAFE 比较样本数被修改。")
    return cast(dict[str, object], payload)


def _wait_job(harness: ProductHarness, job_id: str) -> str:
    """等待合成 DOCX 入库并返回活动 Revision ID。"""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = harness.runtime.sdk.get_job(job_id)
        if job.state.value == "succeeded":
            if job.revision_id is None:
                raise AssertionError("成功入库缺少 Revision ID。")
            return job.revision_id
        if job.state.value not in {"queued", "running"}:
            raise AssertionError(f"合成 DOCX 入库失败：{job.state.value}")
        time.sleep(0.01)
    raise AssertionError("合成 DOCX 入库超时。")


def _prepare_corpus(harness: ProductHarness) -> tuple[str, str, str]:
    """建立各捕获模式完全等价的单 DOCX 离线语料。"""
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    content = build_package(
        "<w:p><w:r><w:t>"
        f"离线性能验收标记是 {_CORPUS_MARKER}，审批后归档。"
        "</w:t></w:r></w:p>"
    )
    job = harness.runtime.sdk.create_document(
        project_id,
        knowledge_base_id,
        display_name="v3-05-performance.docx",
        content=content,
        media_type=_MEDIA_TYPE,
        idempotency_key="v305-performance-document",
    )
    revision_id = _wait_job(harness, job.job_id)
    return project_id, knowledge_base_id, revision_id


def _enable_no_trace_baseline(harness: ProductHarness) -> None:
    """在语料入库后仅替换查询侧 Trace 为测试 Null adapter。"""
    adapter = _HistoryOnlyNullTrace(harness.runtime.history)
    harness.runtime.sdk._query_history = adapter
    harness.runtime.retrieval_runtime.retrieval._trace = adapter
    harness.runtime.p09.prepare_trace = adapter.prepare


def _query_text(
    temperature: str,
    shape: str,
    *,
    concurrency: int,
    index: int,
    measurement_id: str,
) -> str:
    """生成跨捕获模式一致、跨 cell 隔离的合成问题。"""
    suffix = 0 if shape == "identical" else index
    return (
        f"{_CORPUS_MARKER} {temperature} {shape} "
        f"concurrency-{concurrency} sample-{measurement_id} "
        f"item-{suffix} 的归档要求"
    )


def _isolated_client(
    harness: ProductHarness,
    peer_index: int,
) -> tuple[TestClient, dict[str, str]]:
    """为一个测量 cell 创建独立 loopback 主体与管理员 Session。"""
    del peer_index
    client = TestClient(
        create_product_app(harness.runtime),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50_000),
    )
    login = client.post(
        "/api/v1/console/session",
        json={"bootstrap_token": harness.bootstrap_token},
    )
    login.raise_for_status()
    csrf = login.json().get("csrf_token")
    if not isinstance(csrf, str):
        client.close()
        raise AssertionError("性能测量 Session 缺少 CSRF。")
    return client, {"X-CSRF-Token": csrf}


def _close_isolated_client(
    client: TestClient,
    headers: Mapping[str, str],
) -> None:
    """撤销测量 Session 后关闭 TestClient。"""
    response = client.delete(
        "/api/v1/console/session",
        headers=dict(headers),
    )
    if response.status_code != 204:
        raise AssertionError("性能测量 Session 撤销失败。")
    client.close()


def _result_signature(payload: Mapping[str, object]) -> str:
    """仅保留等价性摘要，不把合成正文写入性能报告。"""
    evidence = payload.get("evidence")
    related = payload.get("related_contents")
    evidence_items = evidence if isinstance(evidence, list) else []
    related_items = related if isinstance(related, list) else []
    safe = {
        "answer": payload.get("answer"),
        "confidence": payload.get("confidence"),
        "evidence": [
            {
                "citation_text": item.get("citation_text"),
                "heading_path": item.get("heading_path"),
                "publishable": item.get("publishable"),
                "retrieval_origins": item.get("retrieval_origins"),
                "selection_reason": item.get("selection_reason"),
                "source_label": item.get("source_label"),
            }
            for item in evidence_items
            if isinstance(item, dict)
        ],
        "generation_mode": payload.get("generation_mode"),
        "reason_code": payload.get("reason_code"),
        "related": [
            {
                "excerpt": item.get("excerpt"),
                "heading_path": item.get("heading_path"),
                "relevance_reason": item.get("relevance_reason"),
            }
            for item in related_items
            if isinstance(item, dict)
        ],
        "route_reason_code": payload.get("route_reason_code"),
        "status": payload.get("status"),
    }
    raw = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _request_async(  # noqa: PLR0913, PLR0917
    client: httpx.AsyncClient,
    endpoint: str,
    headers: Mapping[str, str],
    query: str,
    trace_mode: str,
    start: asyncio.Event,
) -> _Sample:
    """从同一 ASGI event loop 并发发起 Product HTTP 请求。"""
    try:
        await start.wait()
        started = time.perf_counter()
        response = await client.post(
            endpoint,
            headers=dict(headers),
            json={
                "query": query,
                "limit": 5,
                "history_mode": "metadata_only",
                "trace_mode": "SAFE" if trace_mode == "NONE" else trace_mode,
            },
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        header_ms = elapsed_ms
        if response.status_code != 200:
            error_code = None
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(
                    body.get("error"), dict
                ):
                    value = body["error"].get("code")
                    error_code = value if isinstance(value, str) else None
            except json.JSONDecodeError:
                error_code = "INVALID_ERROR_JSON"
            return _Sample(
                elapsed_ms=elapsed_ms,
                header_ms=header_ms,
                http_status=response.status_code,
                trace_id=response.headers.get("X-Trace-Id"),
                result_signature=None,
                result_origin=None,
                singleflight_role=None,
                singleflight_wait_ms=0,
                cache_hit=False,
                provider_calls=0,
                error_code=error_code or "HTTP_ERROR",
            )
        raw = response.json()
        if not isinstance(raw, dict):
            raise AssertionError("Product Query 响应不是 JSON object。")
        payload = cast(dict[str, object], raw)
        summary = payload.get("trace_summary")
        provider_calls = (
            int(summary.get("provider_call_count", 0))
            if isinstance(summary, dict)
            else 0
        )
        return _Sample(
            elapsed_ms=elapsed_ms,
            header_ms=header_ms,
            http_status=200,
            trace_id=cast(str | None, payload.get("trace_id")),
            result_signature=_result_signature(payload),
            result_origin=cast(str | None, payload.get("result_origin")),
            singleflight_role=cast(
                str | None,
                payload.get("singleflight_role"),
            ),
            singleflight_wait_ms=float(
                cast(int | float, payload.get("singleflight_wait_ms", 0))
            ),
            cache_hit=bool(payload.get("cache_hit")),
            provider_calls=provider_calls,
            error_code=None,
        )
    except Exception as error:  # 测量必须把线程异常转为明确 FAIL 样本。
        return _Sample(
            elapsed_ms=0,
            header_ms=0,
            http_status=0,
            trace_id=None,
            result_signature=None,
            result_origin=None,
            singleflight_role=None,
            singleflight_wait_ms=0,
            cache_hit=False,
            provider_calls=0,
            error_code=type(error).__name__,
        )


async def _run_http_wave(
    harness: ProductHarness,
    endpoint: str,
    query_batches: Sequence[Sequence[str]],
    *,
    mode: str,
    temperature: str,
) -> _WaveOutcome:
    """在一个真实 ASGI 应用实例上预热并执行一次独立波次。

    Args:
        harness: 当前捕获模式的 Product 测试运行时。
        endpoint: Product 查询 HTTP 路径。
        query_batches: 在同一 Session 内顺序执行的并发请求批次。
        mode: 当前 Trace 捕获模式。
        temperature: cold 或 warm 缓存状态。

    Returns:
        波次样本、逐批结果来源及前后资源指标。

    """
    transport = httpx.ASGITransport(
        app=create_product_app(harness.runtime),
        client=("127.0.0.1", 50_000),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        timeout=30,
    ) as client:
        login = await client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": harness.bootstrap_token},
        )
        login.raise_for_status()
        csrf = login.json().get("csrf_token")
        if not isinstance(csrf, str):
            raise AssertionError("性能测量 Session 缺少 CSRF。")
        headers = {"X-CSRF-Token": csrf}
        try:
            if temperature == "warm":
                warm_queries = dict.fromkeys(
                    query for batch in query_batches for query in batch
                )
                for query in warm_queries:
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        json={
                            "query": query,
                            "limit": 5,
                            "history_mode": "metadata_only",
                            "trace_mode": mode,
                        },
                    )
                    response.raise_for_status()
            harness.runtime.traces.recorder.flush()
            cache_before = harness.runtime.retrieval_runtime.cache.metrics()
            singleflight_before = (
                harness.runtime.profiles.singleflight_metrics()
            )
            samples: list[_Sample] = []
            batch_origins: list[dict[str, int]] = []
            for queries in query_batches:
                start = asyncio.Event()
                tasks = tuple(
                    asyncio.create_task(
                        _request_async(
                            client,
                            endpoint,
                            headers,
                            query,
                            mode,
                            start,
                        )
                    )
                    for query in queries
                )
                await asyncio.sleep(0)
                start.set()
                batch_samples = tuple(await asyncio.gather(*tasks))
                samples.extend(batch_samples)
                batch_origins.append(
                    dict(
                        sorted(
                            Counter(
                                sample.result_origin or "unknown"
                                for sample in batch_samples
                                if sample.http_status == 200
                            ).items()
                        )
                    )
                )
            harness.runtime.traces.recorder.flush()
            cache_after = harness.runtime.retrieval_runtime.cache.metrics()
            singleflight_after = harness.runtime.profiles.singleflight_metrics()
        finally:
            revoked = await client.delete(
                "/api/v1/console/session",
                headers=headers,
            )
            if revoked.status_code != 204:
                raise AssertionError("性能测量 Session 撤销失败。")
    return _WaveOutcome(
        samples=tuple(samples),
        batch_origins=tuple(batch_origins),
        cache_before=cache_before,
        cache_after=cache_after,
        singleflight_before=singleflight_before,
        singleflight_after=singleflight_after,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    """返回非空样本的最近秩百分位。"""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _metric_delta(
    before: RetrievalCacheMetrics | SingleflightMetrics,
    after: RetrievalCacheMetrics | SingleflightMetrics,
) -> dict[str, int]:
    """计算两个整型 dataclass 指标快照的非敏感差值。"""
    before_values = asdict(before)
    after_values = asdict(after)
    return {
        key: int(after_values[key]) - int(value)
        for key, value in before_values.items()
    }


def _integer(value: object, label: str) -> int:
    """严格读取配置或报告中的整数。"""
    if type(value) is not int:
        raise AssertionError(f"{label} 必须是整数。")
    return value


def _number(value: object, label: str) -> float:
    """严格读取配置或报告中的有限数值。"""
    if type(value) not in {int, float}:
        raise AssertionError(f"{label} 必须是数值。")
    number = float(cast(int | float, value))
    if not math.isfinite(number):
        raise AssertionError(f"{label} 必须是有限数值。")
    return number


def _stage_summary(
    harness: ProductHarness,
    trace_ids: Sequence[str],
) -> dict[str, dict[str, float | int]]:
    """从 canonical History 汇总应用阶段耗时。"""
    values: dict[str, list[float]] = defaultdict(list)
    for trace_id in trace_ids:
        diagnostics = harness.runtime.history.diagnostics(trace_id)
        for timing in diagnostics.stage_timings:
            values[timing.stage].append(timing.elapsed_ms)
    return {
        stage: {
            "count": len(samples),
            "p50_ms": round(_percentile(samples, 0.5), 3),
            "p95_ms": round(_percentile(samples, 0.95), 3),
        }
        for stage, samples in sorted(values.items())
    }


def _trace_summary(
    harness: ProductHarness,
    mode: str,
    trace_ids: Sequence[str],
) -> dict[str, object]:
    """汇总 Trace 完整性；NONE 必须没有查询 Operational Trace。"""
    complete = 0
    missing = 0
    dropped_spans = 0
    dropped_decisions = 0
    for trace_id in trace_ids:
        try:
            detail = harness.runtime.traces.detail(trace_id)
        except TraceNotFoundError:
            missing += 1
            continue
        complete += int(detail.trace.capture_complete)
        dropped_spans += detail.trace.dropped_span_count
        dropped_decisions += detail.trace.dropped_decision_count
    return {
        "capture_complete": complete,
        "dropped_decisions": dropped_decisions,
        "dropped_spans": dropped_spans,
        "expected_absent": mode == "NONE",
        "missing": missing,
    }


def _resource_snapshot(harness: ProductHarness) -> dict[str, int]:
    """在计时外读取进程、线程、FD、data/heap 与缓存近似。"""
    cache = harness.runtime.retrieval_runtime.cache.metrics()
    fd_count = len(tuple(Path("/proc/self/fd").iterdir()))
    process_status = _process_status_bytes()
    return {
        "cache_approx_bytes": cache.approx_bytes,
        "cache_entries": cache.entries,
        "fd_count": fd_count,
        "heap_data_approx_bytes": process_status.get("VmData", 0),
        "rss_current_bytes": process_status.get("VmRSS", 0),
        "rss_peak_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "threads": threading.active_count(),
    }


def _process_status_bytes() -> dict[str, int]:
    """读取 Linux proc 状态中的当前 RSS 与 data/heap 近似字节数。"""
    values: dict[str, int] = {}
    for line in (
        Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
    ):
        name, separator, raw = line.partition(":")
        if not separator or name not in {"VmData", "VmRSS"}:
            continue
        fields = raw.split()
        if len(fields) != 2 or fields[1] != "kB":
            raise AssertionError(f"/proc/self/status {name} 格式错误。")
        values[name] = int(fields[0]) * 1024
    if values.keys() != {"VmData", "VmRSS"}:
        raise AssertionError("/proc/self/status 缺少内存字段。")
    return values


def _run_cell_wave(  # noqa: PLR0913
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
    *,
    mode: str,
    temperature: str,
    shape: str,
    concurrency: int,
    wave: int,
    repetitions: int = 1,
) -> _WaveOutcome:
    """执行一个并发 HTTP 波次并在返回前排空后台 Trace writer。"""
    endpoint = (
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:search"
    )
    request_mode = "SAFE" if mode == "NONE" else mode
    query_batches = tuple(
        tuple(
            _query_text(
                temperature,
                shape,
                concurrency=concurrency,
                index=index,
                measurement_id=f"{wave}-{repetition}",
            )
            for index in range(concurrency)
        )
        for repetition in range(repetitions)
    )
    return asyncio.run(
        _run_http_wave(
            harness,
            endpoint,
            query_batches,
            mode=request_mode,
            temperature=temperature,
        )
    )


def _summarize_cell(  # noqa: PLR0913
    harness: ProductHarness,
    *,
    mode: str,
    temperature: str,
    shape: str,
    concurrency: int,
    accumulator: _CellAccumulator,
) -> dict[str, object]:
    """把相邻 A/B 波次聚合为一个不含正文的矩阵 cell。"""
    cache_before = accumulator.cache_before
    cache_after = accumulator.cache_after
    singleflight_before = accumulator.singleflight_before
    singleflight_after = accumulator.singleflight_after
    if (
        cache_before is None
        or cache_after is None
        or singleflight_before is None
        or singleflight_after is None
    ):
        raise AssertionError("性能 cell 没有执行任何波次。")
    samples = tuple(accumulator.samples)
    successful = [sample for sample in samples if sample.http_status == 200]
    trace_ids = [
        sample.trace_id for sample in successful if sample.trace_id is not None
    ]
    latencies = [sample.elapsed_ms for sample in successful]
    header_latencies = [sample.header_ms for sample in successful]
    roles = Counter(sample.singleflight_role or "none" for sample in successful)
    origins = Counter(
        sample.result_origin or "unknown" for sample in successful
    )
    return {
        "cache_delta": _metric_delta(cache_before, cache_after),
        "batches": len(accumulator.batch_origins),
        "concurrency": concurrency,
        "errors": Counter(
            sample.error_code or "none"
            for sample in samples
            if sample.error_code is not None
        ),
        "header_p50_ms": round(_percentile(header_latencies, 0.5), 3)
        if header_latencies
        else None,
        "header_p95_ms": round(_percentile(header_latencies, 0.95), 3)
        if header_latencies
        else None,
        "latency_p50_ms": round(_percentile(latencies, 0.5), 3)
        if latencies
        else None,
        "latency_p95_ms": round(_percentile(latencies, 0.95), 3)
        if latencies
        else None,
        "mode": mode,
        "origins": dict(sorted(origins.items())),
        "provider_calls": sum(sample.provider_calls for sample in successful),
        "request_count": len(samples),
        "resource_after": _resource_snapshot(harness),
        "result_signatures": sorted(
            sample.result_signature
            for sample in successful
            if sample.result_signature is not None
        ),
        "shape": shape,
        "singleflight_delta": _metric_delta(
            singleflight_before,
            singleflight_after,
        ),
        "singleflight_roles": dict(sorted(roles.items())),
        "singleflight_wait_p95_ms": round(
            _percentile(
                [sample.singleflight_wait_ms for sample in successful],
                0.95,
            ),
            3,
        )
        if successful
        else None,
        "stage_timings": _stage_summary(harness, trace_ids),
        "successful": len(successful),
        "temperature": temperature,
        "trace": _trace_summary(harness, mode, trace_ids),
        "wave_latencies": accumulator.wave_latencies,
        "wave_origins": accumulator.wave_origins,
        "waves": len(accumulator.wave_origins),
        "batch_origins": accumulator.batch_origins,
    }


def _run_cell(  # noqa: PLR0913
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
    *,
    mode: str,
    temperature: str,
    shape: str,
    concurrency: int,
    minimum_samples: int,
    minimum_waves: int,
) -> dict[str, object]:
    """执行足以形成稳定 P95 的多轮并发 HTTP 样本。"""
    waves = max(minimum_waves, math.ceil(minimum_samples / concurrency))
    accumulator = _CellAccumulator()
    for wave in range(waves):
        accumulator.add(
            _run_cell_wave(
                harness,
                project_id,
                knowledge_base_id,
                mode=mode,
                temperature=temperature,
                shape=shape,
                concurrency=concurrency,
                wave=wave,
            )
        )
    return _summarize_cell(
        harness,
        mode=mode,
        temperature=temperature,
        shape=shape,
        concurrency=concurrency,
        accumulator=accumulator,
    )


def _comparison_sampling_plan(
    concurrency: int,
    minimum_samples: int,
    minimum_waves: int,
) -> tuple[int, int]:
    """计算尾延迟比较的独立 wave 数与每 wave 重复批次。

    Args:
        concurrency: 每个并发批次的请求数。
        minimum_samples: 每个 NONE/SAFE cell 的最小计时样本数。
        minimum_waves: 每个 cell 的最小独立调度波次数。

    Returns:
        独立 wave 数与每个 wave 内顺序执行的批次数。

    """
    if concurrency <= 0 or minimum_samples <= 0 or minimum_waves <= 0:
        raise AssertionError("性能采样参数必须是正整数。")
    repetitions = max(
        1,
        math.ceil(minimum_samples / (minimum_waves * concurrency)),
    )
    return minimum_waves, repetitions


def _stream_probe(
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
    *,
    mode: str,
    temperature: str,
) -> dict[str, object]:
    """记录真实 SSE 协议首事件、首合法内容和总时延。"""
    endpoint = (
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:answer"
    )
    query = f"{_CORPUS_MARKER} stream {temperature} 的归档要求"
    request_mode = "SAFE" if mode == "NONE" else mode
    peer_index = 100 if temperature == "cold" else 101
    client, headers = _isolated_client(harness, peer_index)
    if temperature == "warm":
        warm = client.post(
            endpoint,
            headers=headers,
            json={
                "query": query,
                "limit": 5,
                "history_mode": "metadata_only",
                "trace_mode": request_mode,
            },
        )
        warm.raise_for_status()
    started = time.perf_counter()
    first_protocol_ms: float | None = None
    first_content_ms: float | None = None
    event_name = ""
    final_seen = False
    try:
        with client.stream(
            "POST",
            endpoint,
            headers=headers,
            json={
                "query": query,
                "limit": 5,
                "history_mode": "metadata_only",
                "stream": True,
                "stream_protocol": "rag-answer-sse-v1",
                "trace_mode": request_mode,
            },
        ) as response:
            header_ms = (time.perf_counter() - started) * 1000
            trace_id = response.headers.get("X-Trace-Id")
            if response.status_code != 200:
                response.read()
                raise AssertionError(
                    "SSE probe HTTP "
                    f"{response.status_code}: {response.text[:500]}"
                )
            for line in response.iter_lines():
                elapsed_ms = (time.perf_counter() - started) * 1000
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip()
                    if first_protocol_ms is None:
                        first_protocol_ms = elapsed_ms
                elif line.startswith("data:") and event_name in {
                    "claim",
                    "final",
                }:
                    if first_content_ms is None:
                        first_content_ms = elapsed_ms
                    final_seen = final_seen or event_name == "final"
    finally:
        _close_isolated_client(client, headers)
    total_ms = (time.perf_counter() - started) * 1000
    return {
        "final_seen": final_seen,
        "first_content_ms": round(first_content_ms, 3)
        if first_content_ms is not None
        else None,
        "first_protocol_ms": round(first_protocol_ms, 3)
        if first_protocol_ms is not None
        else None,
        "header_ms": round(header_ms, 3),
        "mode": mode,
        "temperature": temperature,
        "total_ms": round(total_ms, 3),
        "trace_id_present": bool(trace_id),
    }


def _cancel_probe(
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
) -> dict[str, object]:
    """关闭首事件后的 SSE，并记录终态与已结算 Provider 调用。"""
    endpoint = (
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:answer"
    )
    trace_id: str | None = None
    client, headers = _isolated_client(harness, 120)
    try:
        with client.stream(
            "POST",
            endpoint,
            headers=headers,
            json={
                "query": f"{_CORPUS_MARKER} cancellation probe",
                "limit": 5,
                "history_mode": "metadata_only",
                "stream": True,
                "stream_protocol": "rag-answer-sse-v1",
                "trace_mode": "SAFE",
            },
        ) as response:
            response.raise_for_status()
            trace_id = response.headers.get("X-Trace-Id")
            next(response.iter_lines(), None)
    finally:
        _close_isolated_client(client, headers)
    if trace_id is None:
        return {
            "history_status": "MISSING_TRACE_ID",
            "provider_calls": 0,
        }
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            detail = harness.runtime.history.detail(trace_id)
        except RagError:
            time.sleep(0.01)
            continue
        status = str(detail.get("status"))
        if status not in {"STARTED", "RUNNING"}:
            usage = detail.get("provider_usage")
            provider_calls = 0
            if isinstance(usage, list):
                for item in usage:
                    if not isinstance(item, dict):
                        continue
                    count = item.get("call_count", 0)
                    if type(count) is int:
                        provider_calls += count
            return {
                "history_status": status,
                "provider_calls": provider_calls,
            }
        time.sleep(0.01)
    return {"history_status": "TIMEOUT", "provider_calls": 0}


def _cell_key(cell: Mapping[str, object]) -> tuple[object, ...]:
    """返回 NONE/SAFE 可直接配对的矩阵身份。"""
    return (
        cell["temperature"],
        cell["shape"],
        cell["concurrency"],
    )


def _evaluate(
    cells: Sequence[Mapping[str, object]],
    thresholds: Mapping[str, object],
) -> tuple[bool, list[dict[str, object]]]:
    """按冻结值执行结果等价、Trace 与 P95 非劣判定。"""
    limits = cast(dict[str, object], thresholds["thresholds"])
    checks: list[dict[str, object]] = []

    def add(name: str, passed: bool, detail: object) -> None:
        checks.append({"detail": detail, "name": name, "passed": passed})

    errors = sum(
        _integer(cell["request_count"], "request_count")
        - _integer(cell["successful"], "successful")
        for cell in cells
    )
    add(
        "http_errors",
        errors <= _integer(limits["max_http_errors"], "max_http_errors"),
        errors,
    )
    safe_cells = {
        _cell_key(cell): cell for cell in cells if cell["mode"] == "SAFE"
    }
    none_cells = {
        _cell_key(cell): cell for cell in cells if cell["mode"] == "NONE"
    }
    for key in sorted(safe_cells):
        safe = safe_cells[key]
        baseline = none_cells[key]
        safe_p95 = _number(safe["latency_p95_ms"], "safe_p95")
        baseline_p95 = _number(
            baseline["latency_p95_ms"],
            "baseline_p95",
        )
        allowed = baseline_p95 + max(
            _number(
                limits["safe_p95_over_no_trace_absolute_ms"],
                "absolute_non_regression",
            ),
            baseline_p95
            * _number(
                limits["safe_p95_over_no_trace_relative"],
                "relative_non_regression",
            ),
        )
        add(
            "safe_p95_non_regression:" + ":".join(map(str, key)),
            safe_p95 <= allowed,
            {
                "allowed_ms": round(allowed, 3),
                "baseline_p95_ms": baseline_p95,
                "safe_p95_ms": safe_p95,
            },
        )
        add(
            "safe_result_equivalence:" + ":".join(map(str, key)),
            safe["result_signatures"] == baseline["result_signatures"]
            and safe["provider_calls"] == baseline["provider_calls"],
            {
                "provider_calls_equal": (
                    safe["provider_calls"] == baseline["provider_calls"]
                ),
                "signatures_equal": (
                    safe["result_signatures"] == baseline["result_signatures"]
                ),
            },
        )
    for cell in cells:
        resource_after = cast(dict[str, object], cell["resource_after"])
        add(
            "cache_bound:"
            + ":".join(
                map(
                    str,
                    (cell["mode"], *_cell_key(cell)),
                )
            ),
            _integer(resource_after["cache_entries"], "cache_entries")
            <= _integer(limits["cache_max_entries"], "cache_max_entries"),
            resource_after["cache_entries"],
        )
        if cell["mode"] == "SAFE":
            trace = cast(dict[str, object], cell["trace"])
            dropped = _integer(trace["dropped_spans"], "dropped_spans") + (
                _integer(trace["dropped_decisions"], "dropped_decisions")
            )
            add(
                "safe_trace_complete:" + ":".join(map(str, _cell_key(cell))),
                dropped
                <= _integer(
                    limits["safe_max_dropped_trace_items"],
                    "safe_max_dropped_trace_items",
                )
                and _integer(trace["capture_complete"], "capture_complete")
                == _integer(cell["successful"], "successful"),
                trace,
            )
            if cell["temperature"] == "cold" and cell["shape"] == "identical":
                batch_origins = cast(
                    list[dict[str, int]],
                    cell["batch_origins"],
                )
                fresh_per_batch = [
                    origins.get("fresh", 0) for origins in batch_origins
                ]
                add(
                    "singleflight_one_fresh:" + str(cell["concurrency"]),
                    all(
                        fresh
                        <= _integer(
                            limits["identical_cold_max_fresh_executions"],
                            "identical_cold_max_fresh_executions",
                        )
                        for fresh in fresh_per_batch
                    ),
                    {"fresh_per_batch": fresh_per_batch},
                )
    return all(bool(check["passed"]) for check in checks), checks


def _git_revision() -> tuple[str, bool]:
    """返回当前 Git SHA 及是否含未提交改动。"""
    revision = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["/usr/bin/git", "status", "--porcelain"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return revision, dirty


def _chunk_identity(harness: ProductHarness) -> str:
    """计算当前 Parser、Chunker 与索引策略的固定身份。"""
    components = harness.runtime.retrieval_runtime.persistence.components
    return hashlib.sha256(
        json.dumps(
            {
                "chunker": components.chunker.descriptor.model_dump(
                    mode="json"
                ),
                "index_fingerprint": components.index_fingerprint,
                "parser": components.parser.descriptor.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _prepare_mode_context(root: Path, mode: str) -> _ModeContext:
    """构造模式隔离 Runtime，并建立等价离线语料。"""
    harness = build_product_harness(root / mode.lower())
    try:
        project_id, knowledge_base_id, revision_id = _prepare_corpus(harness)
    except Exception:
        harness.close()
        raise
    if mode == "NONE":
        _enable_no_trace_baseline(harness)
    return _ModeContext(
        mode=mode,
        harness=harness,
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
        revision_id=revision_id,
    )


def _run_paired_baselines(
    root: Path,
    concurrencies: Sequence[int],
    minimum_samples: int,
    minimum_waves: int,
    minimum_comparison_samples: int,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
    list[str],
]:
    """逐波相邻且交替执行 NONE/SAFE，避免顺序和后台写入污染。"""
    contexts: dict[str, _ModeContext] = {}
    cells: list[dict[str, object]] = []
    probes: list[dict[str, object]] = []
    try:
        for mode in ("NONE", "SAFE"):
            contexts[mode] = _prepare_mode_context(root, mode)
        for temperature in ("cold", "warm"):
            cell_specs = tuple(
                (concurrency, shape)
                for concurrency in concurrencies
                for shape in ("unique", "identical")
            )
            for index, (concurrency, shape) in enumerate(cell_specs):
                waves, repetitions = _comparison_sampling_plan(
                    concurrency,
                    max(minimum_samples, minimum_comparison_samples),
                    minimum_waves,
                )
                accumulators = {
                    "NONE": _CellAccumulator(),
                    "SAFE": _CellAccumulator(),
                }
                for wave in range(waves):
                    mode_order = (
                        ("NONE", "SAFE")
                        if (index + wave) % 2 == 0
                        else ("SAFE", "NONE")
                    )
                    for mode in mode_order:
                        context = contexts[mode]
                        accumulators[mode].add(
                            _run_cell_wave(
                                context.harness,
                                context.project_id,
                                context.knowledge_base_id,
                                mode=mode,
                                temperature=temperature,
                                shape=shape,
                                concurrency=concurrency,
                                wave=wave,
                                repetitions=repetitions,
                            )
                        )
                for mode in ("NONE", "SAFE"):
                    context = contexts[mode]
                    cells.append(
                        _summarize_cell(
                            context.harness,
                            mode=mode,
                            temperature=temperature,
                            shape=shape,
                            concurrency=concurrency,
                            accumulator=accumulators[mode],
                        )
                    )
            for mode in ("NONE", "SAFE"):
                context = contexts[mode]
                probes.append(
                    _stream_probe(
                        context.harness,
                        context.project_id,
                        context.knowledge_base_id,
                        mode=mode,
                        temperature=temperature,
                    )
                )
        safe = contexts["SAFE"]
        cancellation = _cancel_probe(
            safe.harness,
            safe.project_id,
            safe.knowledge_base_id,
        )
        assert all(
            context.revision_id.startswith("irev_")
            for context in contexts.values()
        )
        identities = [
            _chunk_identity(context.harness) for context in contexts.values()
        ]
        return cells, probes, cancellation, identities
    finally:
        for context in reversed(tuple(contexts.values())):
            context.harness.close()


def _run_instrumented_mode(
    root: Path,
    mode: str,
    concurrencies: Sequence[int],
    minimum_samples: int,
    minimum_waves: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    """运行一个 DIAGNOSTIC 或 FULL 隔离矩阵。"""
    context = _prepare_mode_context(root, mode)
    cells: list[dict[str, object]] = []
    probes: list[dict[str, object]] = []
    try:
        for temperature in ("cold", "warm"):
            cells.extend(
                _run_cell(
                    context.harness,
                    context.project_id,
                    context.knowledge_base_id,
                    mode=mode,
                    temperature=temperature,
                    shape=shape,
                    concurrency=concurrency,
                    minimum_samples=minimum_samples,
                    minimum_waves=minimum_waves,
                )
                for concurrency in concurrencies
                for shape in ("unique", "identical")
            )
            probes.append(
                _stream_probe(
                    context.harness,
                    context.project_id,
                    context.knowledge_base_id,
                    mode=mode,
                    temperature=temperature,
                )
            )
        assert context.revision_id.startswith("irev_")
        return cells, probes, _chunk_identity(context.harness)
    finally:
        context.harness.close()


def _run_matrix(tmp_path: Path) -> dict[str, object]:
    """在四个隔离 Runtime 上运行冻结矩阵并返回规范报告。"""
    thresholds = _load_thresholds()
    capture_modes = cast(list[str], thresholds["capture_modes"])
    concurrencies = cast(list[int], thresholds["concurrencies"])
    minimum_samples = _integer(
        thresholds["minimum_samples_per_cell"],
        "minimum_samples_per_cell",
    )
    minimum_waves = _integer(
        thresholds["minimum_waves_per_cell"],
        "minimum_waves_per_cell",
    )
    minimum_comparison_waves = _integer(
        thresholds["minimum_comparison_waves_per_cell"],
        "minimum_comparison_waves_per_cell",
    )
    minimum_comparison_samples = _integer(
        thresholds["minimum_comparison_samples_per_cell"],
        "minimum_comparison_samples_per_cell",
    )
    cells: list[dict[str, object]] = []
    stream_probes: list[dict[str, object]] = []
    cancellation: dict[str, object] | None = None
    chunk_identities: list[str] = []
    paired_cells, paired_probes, cancellation, paired_identities = (
        _run_paired_baselines(
            tmp_path,
            concurrencies,
            minimum_samples,
            minimum_comparison_waves,
            minimum_comparison_samples,
        )
    )
    cells.extend(paired_cells)
    stream_probes.extend(paired_probes)
    chunk_identities.extend(paired_identities)
    for mode in capture_modes:
        if mode in {"NONE", "SAFE"}:
            continue
        mode_cells, mode_probes, identity = _run_instrumented_mode(
            tmp_path,
            mode,
            concurrencies,
            minimum_samples,
            minimum_waves,
        )
        cells.extend(mode_cells)
        stream_probes.extend(mode_probes)
        chunk_identities.append(identity)
    passed, checks = _evaluate(cells, thresholds)
    revision, dirty = _git_revision()
    return {
        "cancellation_probe": cancellation,
        "cells": cells,
        "checks": checks,
        "chunk_identity_sha256": sorted(set(chunk_identities)),
        "comparison": {
            "main": "NOT_COMPARABLE",
            "reason": (
                "未在同语料、同配置、同硬件、同缓存条件重跑 main/Industry"
            ),
        },
        "environment": {
            "cpu_count": os.cpu_count(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "matrix_transport": "HTTPX AsyncClient ASGITransport",
            "stream_transport": "Starlette TestClient ASGI HTTP",
        },
        "passed": passed
        and all(bool(item["final_seen"]) for item in stream_probes),
        "report_schema": "v3-05-product-http-performance-v4",
        "source_revision": revision,
        "stream_probes": stream_probes,
        "thresholds": thresholds,
        "thresholds_sha256": hashlib.sha256(
            _THRESHOLDS.read_bytes()
        ).hexdigest(),
        "working_tree_dirty": dirty,
    }


def _run_isolated_performance_gate(tmp_path: Path) -> None:
    """在独立 pytest 进程运行矩阵，隔离前序测试的进程状态。

    Args:
        tmp_path: 父测试用于接收子进程收据的隔离目录。

    Returns:
        子进程完整执行冻结矩阵并通过时无返回值。

    """
    configured_output = os.environ.get("RAG_V305_PERFORMANCE_OUTPUT")
    report_path = (
        (tmp_path / "product-http.json").resolve()
        if configured_output is None
        else Path(configured_output).resolve()
    )
    environment = os.environ.copy()
    environment[_ISOLATED_CHILD_ENV] = "1"
    environment["RAG_V305_PERFORMANCE_OUTPUT"] = str(report_path)
    environment.pop("PYTEST_CURRENT_TEST", None)
    completed = subprocess.run(  # noqa: S603 - 仅调用当前可信解释器。
        (
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).resolve()}::"
            "test_v3_05_product_http_performance_gate",
            "-q",
        ),
        cwd=_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    failure_output = "\n".join(
        part[-8000:] for part in (completed.stdout, completed.stderr) if part
    )
    assert completed.returncode == 0, failure_output
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert isinstance(report, dict) and report.get("passed") is True


def test_v3_05_product_http_performance_gate(tmp_path: Path) -> None:
    """执行默认矩阵、可选写收据，并使冻结非劣门槛 fail-closed。"""
    if os.environ.get(_ISOLATED_CHILD_ENV) != "1":
        _run_isolated_performance_gate(tmp_path)
        return
    report = _run_matrix(tmp_path)
    output_name = os.environ.get("RAG_V305_PERFORMANCE_OUTPUT")
    if output_name:
        output = Path(output_name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    failed = [
        check
        for check in cast(list[dict[str, object]], report["checks"])
        if not check["passed"]
    ]
    assert report["passed"], json.dumps(
        failed,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_comparison_sampling_plan_balances_tail_samples() -> None:
    """验证低并发通过顺序 batch 补样且保留独立 wave 数。"""
    expected = {1: (40, 8), 8: (40, 1), 16: (40, 1)}
    for concurrency, plan in expected.items():
        waves, repetitions = _comparison_sampling_plan(
            concurrency,
            minimum_samples=320,
            minimum_waves=40,
        )
        assert (waves, repetitions) == plan
        assert waves * repetitions * concurrency >= 320
