"""湾事通管理员隔离候选问答入口。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from queue import Empty, Queue
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from rag_app.application.answering.natural_answer import NaturalAnswerResult
from rag_app.clients.resilience import StreamCancellation
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.query_executor import QueryAdmissionError
from rag_app.wanshitong.models import ScopeBinding
from rag_app.wanshitong.scope_service import FixedScopeService

_BASE = "/api/v1/admin/wanshitong"
_FINAL_CHUNK_CHARS = 256
_CANDIDATE_WAIT_SECONDS = 240


class CandidateChatRequest(FrozenModel):
    """只在管理员测试入口启用的通用问答输入。"""

    query: str = Field(min_length=1, max_length=8000)
    engine_id: Literal["wk-standard-v1", "wk-standard-pc-v1"] = "wk-standard-v1"
    conversation_context: tuple[str, ...] = Field(default=(), max_length=8)
    limit: int = Field(default=10, ge=1, le=50)


def _event(name: str, payload: dict[str, object]) -> str:
    """编码独立候选 SSE 事件。"""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def register_candidate_routes(  # noqa: PLR0915
    app: FastAPI,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
    admin_scope: Callable[[Request, FixedScopeService], ScopeBinding],
) -> None:
    """注册不改变公共 SSE 协议的管理员候选流。"""

    @app.post(_BASE + "/candidate/chat", tags=["wanshitong-admin"])
    def _candidate_chat(  # noqa: PLR0915
        body: CandidateChatRequest, request: Request
    ) -> StreamingResponse:
        binding = admin_scope(request, scope_service)
        runtime.sdk.require_active_knowledge_base(
            binding.project_id, binding.knowledge_base_id
        )
        trace_id = f"trace_{uuid.uuid4().hex}"
        owner = (
            "wk-admin-"
            + hashlib.sha256(
                str(
                    getattr(request.state, "product_session_id", "legacy_admin")
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        scope = KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        query = SearchRequest(
            scope=scope,
            text=body.query,
            limit=body.limit,
            conversation_context=body.conversation_context,
            owner_identity=owner,
            trace_id=trace_id,
            singleflight_enabled=False,
        )
        audit = QueryAuditContext(
            trace_id=trace_id,
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            owner_id=owner,
            deployment_id=None,
            identity_source="ADMIN_SESSION",
            traffic_class="EVALUATION",
            classification_source="WEKNORA_CANDIDATE_ENDPOINT",
        )
        cancellation = StreamCancellation()
        completed: Queue[
            tuple[NaturalAnswerResult | None, RagError | None, bool]
        ] = Queue(maxsize=1)

        def work() -> None:
            """在现有四并发查询执行器内完成授权、检索与结算。"""
            result: NaturalAnswerResult | None = None
            failure: RagError | None = None
            cancelled = False
            started = False
            try:
                runtime.traces.start(
                    trace_id,
                    scope,
                    body.query,
                    owner_id=owner,
                    save_body=True,
                    audit_context=audit,
                )
                started = True
                with runtime.profiles.retrieval_service_lease(
                    binding.knowledge_base_id,
                    runtime.retrieval_runtime.retrieval,
                ) as service:
                    snapshot = service._query_snapshot(query)
                    frozen = query.model_copy(
                        update={
                            "expected_active_revision_id": (
                                snapshot.revision.index_revision_id
                            ),
                            "expected_serving_fingerprint": (
                                snapshot.serving_fingerprint
                            ),
                        }
                    )
                    with runtime.profiles.query_retrieval_scope(
                        binding.knowledge_base_id,
                        snapshot.revision.index_revision_id,
                    ):
                        result = service.search_natural(
                            frozen,
                            engine_id=body.engine_id,
                            cancellation=cancellation,
                        )
            except QueryCancelled:
                cancelled = True
            except RagError as error:
                failure = error
            except Exception:
                failure = RagError(
                    "候选查询执行失败。",
                    stage="wanshitong.candidate",
                    code="CANDIDATE_INTERNAL_ERROR",
                    trace_id=trace_id,
                )
            finally:
                if started:
                    try:
                        runtime.traces.finish(
                            trace_id,
                            result=result,
                            error=failure,
                            cancelled=cancelled,
                        )
                    except RagError as error:
                        failure = failure or error
                    except Exception:
                        failure = failure or RagError(
                            "候选查询终态保存失败。",
                            stage="wanshitong.candidate.trace",
                            code="CANDIDATE_TRACE_FAILED",
                            trace_id=trace_id,
                        )
                completed.put((result, failure, cancelled))

        try:
            runtime.p09.query_executor.submit(work)
        except QueryAdmissionError as error:
            raise RagError(
                "查询容量已满，请稍后重试。",
                stage="query.admission",
                code="QUEUE_LIMIT_EXCEEDED",
                retryable=True,
                trace_id=trace_id,
            ) from error

        def stream() -> Iterator[str]:
            """最终校验后才把正文按候选协议输出。"""
            try:
                try:
                    result, failure, cancelled = completed.get(
                        timeout=_CANDIDATE_WAIT_SECONDS
                    )
                except Empty:
                    yield _event(
                        "error",
                        {
                            "trace_id": trace_id,
                            "code": "CANDIDATE_TIMEOUT",
                            "message": "候选查询超时，请稍后重试。",
                        },
                    )
                    return
                if cancelled:
                    yield _event(
                        "error", {"trace_id": trace_id, "code": "CANCELLED"}
                    )
                    return
                if failure is not None:
                    yield _event(
                        "error",
                        {
                            "trace_id": trace_id,
                            "code": failure.code,
                            "stage": failure.stage,
                            "message": failure.safe_message,
                        },
                    )
                    return
                if result is None:
                    yield _event(
                        "error",
                        {"trace_id": trace_id, "code": "EMPTY_RESULT"},
                    )
                    return
                answer = result.answer or ""
                for offset in range(0, len(answer), _FINAL_CHUNK_CHARS):
                    yield _event(
                        "answer_delta",
                        {"text": answer[offset : offset + _FINAL_CHUNK_CHARS]},
                    )
                yield _event(
                    "references",
                    {
                        "items": [
                            item.model_dump(mode="json")
                            for item in result.references
                        ]
                    },
                )
                yield _event("final", result.model_dump(mode="json"))
            finally:
                cancellation.cancel()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )


__all__ = ["CandidateChatRequest", "register_candidate_routes"]
