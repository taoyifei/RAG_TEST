import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import httpx
from fastapi import FastAPI
from qdrant_client import QdrantClient

from rag_app.health import ReadinessService
from rag_app.index.job_runner import IndexJobRunner
from rag_app.query_executor import QueryExecutor
from rag_app.runtime import RuntimeBundle
from rag_app.settings import RuntimeSettings
from rag_app.state import StateStore
from rag_app.worker_runtime import WorkerRuntimeBundle


class _Closable:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def close(self) -> None:
        self._calls.append(self._name)


def test_runtime_close_drains_executor_before_network_and_is_idempotent(
) -> None:
    calls: list[str] = []
    bundle = RuntimeBundle(
        app=cast(FastAPI, object()),
        settings=cast(RuntimeSettings, object()),
        qdrant=cast(QdrantClient, _Closable("qdrant", calls)),
        http_clients=(
            cast(httpx.Client, _Closable("http-1", calls)),
            cast(httpx.Client, _Closable("http-2", calls)),
        ),
        readiness=cast(
            ReadinessService,
            _Closable("readiness", calls),
        ),
        query_executor=cast(
            QueryExecutor,
            _Closable("executor", calls),
        ),
    )

    bundle.close()
    bundle.close()

    assert calls == [
        "executor",
        "readiness",
        "http-1",
        "http-2",
        "qdrant",
    ]


def test_worker_close_uses_reverse_construction_order_and_is_idempotent(
) -> None:
    calls: list[str] = []
    bundle = WorkerRuntimeBundle(
        runner=cast(IndexJobRunner, object()),
        control=cast(StateStore, object()),
        qdrant=cast(QdrantClient, _Closable("qdrant", calls)),
        http_client=cast(
            httpx.Client,
            _Closable("http", calls),
        ),
        ocr_http_client=cast(
            httpx.Client,
            _Closable("ocr-http", calls),
        ),
    )

    bundle.close()
    bundle.close()

    assert calls == ["ocr-http", "http", "qdrant"]


def test_runtime_waits_for_active_query_before_closing_network() -> None:
    calls: list[str] = []
    release = threading.Event()
    executor = QueryExecutor(queue_wait_seconds=1.0)

    def query() -> None:
        release.wait()

    executor.submit(query)
    bundle = RuntimeBundle(
        app=cast(FastAPI, object()),
        settings=cast(RuntimeSettings, object()),
        qdrant=cast(QdrantClient, _Closable("qdrant", calls)),
        http_clients=(
            cast(httpx.Client, _Closable("http", calls)),
        ),
        readiness=cast(
            ReadinessService,
            _Closable("readiness", calls),
        ),
        query_executor=executor,
    )

    with ThreadPoolExecutor(max_workers=1) as caller:
        closing = caller.submit(bundle.close)
        time.sleep(0.05)
        assert not closing.done()
        assert calls == []
        release.set()
        closing.result(timeout=1)

    assert calls == ["readiness", "http", "qdrant"]
