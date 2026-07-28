"""在真实 Qdrant 上构建 10 万 synthetic chunk 并测 dense p95。"""

from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from pathlib import Path
from typing import cast

from qdrant_client import QdrantClient
from qdrant_client.http import models


def main() -> int:
    """运行可重复的容量基准并写出 JSON 证据。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        达到延迟阈值时返回 0，否则返回 1。

    """
    arguments = _arguments()
    client = QdrantClient(
        url=arguments.url,
        api_key=arguments.api_key,
        timeout=60,
        check_compatibility=False,
    )
    collection = arguments.collection or f"rag-bench-{uuid.uuid4().hex}"
    started = time.monotonic()
    try:
        _create_collection(client, collection, arguments.dimension)
        _ingest(
            client,
            collection,
            count=arguments.count,
            dimension=arguments.dimension,
            batch_size=arguments.batch_size,
        )
        _wait_ready(client, collection, timeout_seconds=600)
        latencies = _measure(
            client,
            collection,
            dimension=arguments.dimension,
            queries=arguments.queries,
        )
        report = {
            "collection": collection,
            "count": arguments.count,
            "dimension": arguments.dimension,
            "queries": arguments.queries,
            "p50_ms": round(_percentile(latencies, 0.50), 3),
            "p95_ms": round(_percentile(latencies, 0.95), 3),
            "max_ms": round(max(latencies), 3),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "threshold_ms": 500,
        }
        report["passed"] = report["p95_ms"] <= report["threshold_ms"]
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] else 1
    finally:
        if arguments.delete_after and client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:6333")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--collection")
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evaluation/results/qdrant-100k.json"),
    )
    parser.add_argument("--delete-after", action="store_true")
    return parser.parse_args()


def _create_collection(
    client: QdrantClient,
    collection: str,
    dimension: int,
) -> None:
    if client.collection_exists(collection):
        raise ValueError("基准 collection 已存在，拒绝覆盖。")
    client.create_collection(
        collection_name=collection,
        vectors_config={
            "dense": models.VectorParams(
                size=dimension,
                distance=models.Distance.COSINE,
            )
        },
        on_disk_payload=True,
    )
    client.create_payload_index(
        collection,
        field_name="version_state",
        field_schema=models.PayloadSchemaType.KEYWORD,
        wait=True,
    )


def _ingest(
    client: QdrantClient,
    collection: str,
    *,
    count: int,
    dimension: int,
    batch_size: int,
) -> None:
    if min(count, dimension, batch_size) <= 0:
        raise ValueError("count、dimension 与 batch_size 必须为正数。")
    for start in range(0, count, batch_size):
        stop = min(count, start + batch_size)
        integer_ids = list(range(start, stop))
        ids = cast(
            list[models.ExtendedPointId],
            integer_ids,
        )
        vectors = [_vector(point_id, dimension) for point_id in integer_ids]
        payloads = [
            {
                "chunk_id": f"synthetic-{point_id:06d}",
                "version_state": "active",
            }
            for point_id in integer_ids
        ]
        client.upsert(
            collection_name=collection,
            points=models.Batch(
                ids=ids,
                vectors=cast(
                    models.BatchVectorStruct,
                    {"dense": vectors},
                ),
                payloads=payloads,
            ),
            wait=True,
        )
        if stop % 10_000 == 0 or stop == count:
            print(f"ingested={stop}", flush=True)


def _vector(point_id: int, dimension: int) -> list[float]:
    vector = [0.0] * dimension
    vector[point_id % dimension] = 1.0
    vector[(point_id * 31 + 7) % dimension] += 0.5
    return vector


def _wait_ready(
    client: QdrantClient,
    collection: str,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        info = client.get_collection(collection)
        if info.status == models.CollectionStatus.GREEN:
            return
        time.sleep(1)
    raise TimeoutError("Qdrant collection 未在时限内转为 green。")


def _measure(
    client: QdrantClient,
    collection: str,
    *,
    dimension: int,
    queries: int,
) -> list[float]:
    if queries <= 0:
        raise ValueError("queries 必须为正数。")
    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="version_state",
                match=models.MatchValue(value="active"),
            )
        ]
    )
    for index in range(10):
        client.query_points(
            collection,
            query=_vector(index, dimension),
            using="dense",
            query_filter=query_filter,
            limit=20,
            with_payload=False,
            with_vectors=False,
        )
    latencies: list[float] = []
    for index in range(queries):
        started = time.perf_counter()
        client.query_points(
            collection,
            query=_vector(index, dimension),
            using="dense",
            query_filter=query_filter,
            limit=20,
            with_payload=False,
            with_vectors=False,
        )
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
