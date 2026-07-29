"""不记录问题、原文、回答或密钥的结构化审计日志。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from rag_app.query_service import QueryOutcome, StageEvent
from rag_app.state.models import Job

__all__ = ["StructuredAuditLogger"]


@dataclass(frozen=True, slots=True)
class StructuredAuditLogger:
    """仅通过固定字段方法写一行 JSON 审计日志。"""

    logger: logging.Logger
    pipeline_fingerprint: str
    serving_fingerprint: str | None = None

    def query_stage(self, event: StageEvent) -> None:
        """记录不含业务内容的查询阶段。

        Args:
            event: 非敏感阶段事件。

        Returns:
            无返回值。

        """
        self._write(
            {
                "event": "query_stage",
                "trace_id": event.trace_id,
                "stage": event.stage.value,
                "total_elapsed_ms": event.elapsed_ms,
                "metrics": event.metrics,
            }
        )

    def query_outcome(self, outcome: QueryOutcome) -> None:
        """记录回答状态、引用 chunk 和外部调用元数据。

        Args:
            outcome: 已完成引用校验的查询结果。

        Returns:
            无返回值。

        """
        chunk_ids = sorted(
            {
                support.chunk_id
                for claim in outcome.answer.claims
                for support in claim.supports
            }
        )
        self._write(
            {
                "event": "query_outcome",
                "trace_id": outcome.trace_id,
                "status": outcome.answer.status.value,
                "refusal_reason": (
                    None
                    if outcome.answer.refusal_code is None
                    else outcome.answer.refusal_code.value
                ),
                "chunk_ids": chunk_ids,
                "model_calls": outcome.answer.model_calls,
            }
        )
        for call in outcome.calls:
            self._write(
                {
                    "event": "external_call",
                    "trace_id": outcome.trace_id,
                    "endpoint": _sanitize_endpoint(call.endpoint),
                    "retry_count": call.retry_count,
                    "elapsed_ms": round(call.elapsed_seconds * 1000),
                }
            )

    def query_failed(self, trace_id: str, error_code: str) -> None:
        """记录不含异常文本的查询失败码。

        Args:
            trace_id: 本次请求的追踪标识。
            error_code: 稳定且不含正文的失败类别。

        Returns:
            无返回值。

        """
        self._write(
            {
                "event": "query_failed",
                "trace_id": trace_id,
                "error_code": error_code,
            }
        )

    def trace_failure(self, trace_id: str, error_code: str) -> None:
        """记录不含业务正文的 Trace 子系统失败。

        Args:
            trace_id: 当前查询追踪标识。
            error_code: 稳定 Trace 失败码。

        Returns:
            无返回值。

        """
        self._write(
            {
                "event": "trace_failure",
                "trace_id": trace_id,
                "error_code": error_code,
            }
        )

    def index_job(self, job: Job) -> None:
        """记录不含幂等键与租约身份的索引任务状态。

        Args:
            job: 当前索引任务状态。

        Returns:
            无返回值。

        """
        self._write(
            {
                "event": "index_job",
                "job_id": job.job_id,
                "kind": job.kind.value,
                "state": job.state.value,
                "attempt": job.attempt,
                "error_code": job.error_code,
            }
        )

    def _write(self, fields: dict[str, object]) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "pipeline_fingerprint": self.pipeline_fingerprint,
            **fields,
        }
        if self.serving_fingerprint is not None:
            record["serving_fingerprint"] = self.serving_fingerprint
        self.logger.info(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


def _sanitize_endpoint(endpoint: str) -> str:
    """移除 URL 用户信息、query 与 fragment。"""
    parsed = urlsplit(endpoint)
    if not parsed.scheme or parsed.hostname is None:
        return "invalid-endpoint"
    port = "" if parsed.port is None else f":{parsed.port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.hostname}{port}{path}"
