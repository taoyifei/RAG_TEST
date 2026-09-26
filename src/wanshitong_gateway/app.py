"""湾事通独立业务网关：身份与传输，原生 WeKnora 完成问答。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from rag_app.wanshitong.public_models import (
    PublicChatStopRequest,
    PublicFeedbackRequest,
)
from rag_app.wanshitong.shortcuts import SHORTCUT_CATALOG
from wanshitong_gateway.auth import GatewayAuth, register_auth_routes
from wanshitong_gateway.engine_settings import (
    EngineSettings,
    load_server_secret,
)
from wanshitong_gateway.legacy import LegacyHistoryReader
from wanshitong_gateway.models import GatewayChatRequest
from wanshitong_gateway.settings import GatewayAuthSettings
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminClient
from wanshitong_gateway.weknora.client import NativeHttpError, WeKnoraClient
from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner
from wanshitong_gateway.weknora.references import (
    public_citation,
    reference_id,
    resource_handle,
)
from wanshitong_gateway.weknora.stream import (
    PROTOCOL,
    BridgeStream,
    NativeSseDecoder,
)

_MAX_HISTORY_SESSIONS = 50
_MAX_OPS_LIST_LIMIT = 1000
_MAX_OPS_OFFSET = 1_000_000
_MAX_OPS_QUERY_CHARS = 200
_MAX_RECOMMENDATION_ID_LENGTH = 128
_MAX_SOURCE_ID_LENGTH = 128
_MAX_RESOURCE_BYTES = 50 * 1024 * 1024
_MAX_ADMIN_BODY_BYTES = 50 * 1024 * 1024
_HTTP_REDIRECT_START = 300
_HTTP_REDIRECT_END = 400
_HTTP_FORBIDDEN = 403
_HTTP_OK = 200
_MAX_RESOURCE_HANDLE_CHARS = 4096
_INLINE_IMAGE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
    }
)


class _TraceExportRequest(BaseModel):
    """有正文导出必须由管理员对具体对象作二次确认。"""

    model_config = ConfigDict(extra="forbid")

    include_content: bool = False
    confirm_id: str | None = None


class _FeedbackReviewRequest(BaseModel):
    """管理员人工复核，不修改原生问答结果。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "in_review", "resolved"]
    note: str = Field(default="", max_length=2000)
    root_cause: str = Field(default="", max_length=100)
    fix_reference: str = Field(default="", max_length=500)
    verification_references: list[str] = Field(
        default_factory=list, max_length=20
    )
    evaluation_candidate: bool = False


class _RecommendationRequest(BaseModel):
    """人工编辑公开问题；来源 ID 是运营核对记录，不影响检索范围。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    question: str = Field(min_length=1, max_length=500)
    topic_key: str = Field(min_length=1, max_length=100)
    source_knowledge_ids: list[str] = Field(default_factory=list, max_length=20)
    review_note: str = Field(default="", max_length=1000)
    state: Literal["DRAFT", "APPROVED", "DISABLED"] = "DRAFT"
    review_confirmed: bool = False
    disabled_reason: str | None = Field(default=None, max_length=500)


def _recommendation_fields(body: _RecommendationRequest) -> dict[str, Any]:
    """审核通过前需要明确的资料核对记录；不进行旧检索或答案预判。"""
    question = body.question.strip()
    topic = body.topic_key.strip()
    note = body.review_note.strip()
    sources = [item.strip() for item in body.source_knowledge_ids]
    if (
        not question
        or not topic
        or any(
            not item or len(item) > _MAX_SOURCE_ID_LENGTH for item in sources
        )
    ):
        raise HTTPException(
            status_code=422, detail="invalid recommendation fields"
        )
    if len(sources) != len(set(sources)):
        raise HTTPException(
            status_code=422, detail="duplicate knowledge source"
        )
    if body.state == "APPROVED" and (
        not body.review_confirmed or not sources or not note
    ):
        raise HTTPException(
            status_code=422, detail="review confirmation required"
        )
    reason = body.disabled_reason.strip() if body.disabled_reason else None
    if body.state == "DISABLED" and not reason:
        raise HTTPException(status_code=422, detail="disabled reason required")
    return {
        "question": question,
        "topic_key": topic,
        "source_knowledge_ids": sources,
        "review_note": note,
        "state": body.state,
        "disabled_reason": reason if body.state == "DISABLED" else None,
    }


def create_app(  # noqa: PLR0913, PLR0915
    *,
    auth: GatewayAuth | None = None,
    store: GatewayStore | None = None,
    engine: EngineSettings | None = None,
    native: WeKnoraClient | None = None,
    admin_native: NativeAdminClient | None = None,
    legacy: LegacyHistoryReader | None = None,
) -> FastAPI:
    """构建不初始化旧 Parser、索引、检索或答案校验器的 ASGI 应用。"""
    auth = auth or GatewayAuth.from_settings(
        GatewayAuthSettings.from_environment()
    )
    engine = engine or EngineSettings.from_environment()
    store = store or GatewayStore(auth.settings.database_path)
    native = native or WeKnoraClient(
        base_url=engine.base_url,
        api_key=load_server_secret(engine.api_key_file),
        signer=ExternalPrincipalSigner(
            secret=load_server_secret(engine.external_signing_key_file),
            tenant_id=engine.tenant_id,
            deployment_id=auth.settings.deployment_id,
        ),
    )
    admin_native = admin_native or NativeAdminClient(
        base_url=engine.base_url,
        email=engine.admin_email,
        password=load_server_secret(engine.admin_password_file),
        tenant_id=engine.tenant_id,
    )
    if legacy is None:
        legacy_db = os.environ.get("WANSHITONG_LEGACY_SNAPSHOT_FILE", "")
        legacy_key = os.environ.get("WANSHITONG_LEGACY_MASTER_KEY_FILE", "")
        if bool(legacy_db) != bool(legacy_key):
            raise ValueError("旧历史快照和密钥必须同时配置。")
        if legacy_db:
            legacy = LegacyHistoryReader(Path(legacy_db), Path(legacy_key))
    locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await native.close()
            await admin_native.close()

    app = FastAPI(root_path=auth.settings.root_path, lifespan=lifespan)
    register_auth_routes(app, auth)

    @app.get("/api/engine-admin/bootstrap")
    async def engine_admin_bootstrap(request: Request) -> dict[str, Any]:
        """用独立湾事通管理员会话换取原生 UI 所需身份摘要。"""
        principal = auth.require_admin(request)
        try:
            identity = await admin_native.identity()
        except NativeHttpError as error:
            store.record_admin_action(
                actor=principal.audit_actor,
                method="GET",
                path="bootstrap",
                status_code=error.status_code,
            )
            raise HTTPException(
                status_code=502, detail="native admin unavailable"
            ) from None
        store.record_admin_action(
            actor=principal.audit_actor,
            method="GET",
            path="bootstrap",
            status_code=200,
        )
        return {"success": True, "data": identity}

    @app.api_route(
        "/api/engine-admin/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def engine_admin_proxy(path: str, request: Request) -> Response:
        """对管理员放行有限原生路由，令牌始终留在网关内存。"""
        principal = auth.require_admin(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_ADMIN_BODY_BYTES:
                    raise HTTPException(
                        status_code=413, detail="body too large"
                    )
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid content length"
                ) from None
        content = await request.body()
        if len(content) > _MAX_ADMIN_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
        status_code = 502
        try:
            upstream = await admin_native.proxy(
                method=request.method,
                path=path,
                query=request.url.query,
                content=content,
                browser_headers=request.headers,
            )
            status_code = upstream.status_code
            if _HTTP_REDIRECT_START <= status_code < _HTTP_REDIRECT_END:
                if upstream.stream is not None:
                    await upstream.stream.aclose()
                raise HTTPException(
                    status_code=502, detail="native redirect denied"
                )
            if upstream.stream is not None:
                return StreamingResponse(
                    upstream.stream,
                    status_code=status_code,
                    headers=upstream.headers,
                    background=BackgroundTask(upstream.stream.aclose),
                )
            return Response(
                content=upstream.content,
                status_code=status_code,
                headers=upstream.headers,
            )
        except NativeHttpError as error:
            status_code = error.status_code
            raise HTTPException(
                status_code=_HTTP_FORBIDDEN
                if status_code == _HTTP_FORBIDDEN
                else 502,
                detail="native admin request denied or unavailable",
            ) from None
        finally:
            store.record_admin_action(
                actor=principal.audit_actor,
                method=request.method,
                path=path,
                status_code=status_code,
            )

    @app.get("/live")
    def live() -> dict[str, str]:
        """进程存活检查不访问旧系统或生产数据。"""
        return {"status": "alive", "engine": "weknora"}

    @app.get("/api/admin/ops/traces")
    def admin_traces(
        request: Request,
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        feedback_only: bool = False,
    ) -> dict[str, Any]:
        """按本候选部署列出真实网关 Trace，不混入旧引擎。"""
        auth.require_admin(request)
        if (
            not 1 <= limit <= _MAX_OPS_LIST_LIMIT
            or not 0 <= offset <= _MAX_OPS_OFFSET
            or len(query) > _MAX_OPS_QUERY_CHARS
        ):
            raise HTTPException(status_code=422, detail="invalid trace filters")
        query = query.strip()
        return {
            "engine": "weknora",
            "items": store.list_traces(
                deployment_id=auth.settings.deployment_id,
                limit=limit,
                offset=offset,
                query=query,
                feedback_only=feedback_only,
            ),
            "total": store.count_traces(
                deployment_id=auth.settings.deployment_id,
                query=query,
                feedback_only=feedback_only,
            ),
        }

    @app.get("/api/admin/ops/summary")
    def admin_summary(request: Request) -> dict[str, Any]:
        """只汇总本候选实际产生的新引擎问答。"""
        auth.require_admin(request)
        return {
            "engine": "weknora",
            **store.ops_summary(deployment_id=auth.settings.deployment_id),
        }

    @app.get("/api/admin/ops/questions")
    def admin_questions(request: Request) -> dict[str, Any]:
        """给运营页提供近七天原问题精确重复统计。"""
        auth.require_admin(request)
        return {
            "engine": "weknora",
            "window_days": 7,
            "grouping": "exact_trimmed_question",
            "items": store.frequent_questions(
                deployment_id=auth.settings.deployment_id
            ),
        }

    @app.get("/api/admin/ops/recommendations")
    def admin_recommendations(request: Request) -> dict[str, Any]:
        """列出候选环境的人工推荐题及其审核状态。"""
        auth.require_admin(request)
        return {
            "items": store.list_recommendations(
                deployment_id=auth.settings.deployment_id
            )
        }

    @app.post("/api/admin/ops/recommendations")
    def create_recommendation(
        body: _RecommendationRequest, request: Request
    ) -> dict[str, Any]:
        """从原问题新建草稿，或经人工核对后发布题面。"""
        principal = auth.require_admin(request)
        if body.expected_version is not None:
            raise HTTPException(
                status_code=422, detail="new recommendation has no version"
            )
        item = store.save_recommendation(
            deployment_id=auth.settings.deployment_id,
            recommendation_id=None,
            expected_version=None,
            **_recommendation_fields(body),
        )
        store.record_admin_action(
            actor=principal.audit_actor,
            method="POST",
            path="recommendation-create",
            status_code=200,
        )
        return item

    @app.put("/api/admin/ops/recommendations/{recommendation_id}")
    def update_recommendation(
        recommendation_id: str, body: _RecommendationRequest, request: Request
    ) -> dict[str, Any]:
        """乐观锁保护运营编辑与下架。"""
        principal = auth.require_admin(request)
        if body.expected_version is None:
            raise HTTPException(status_code=422, detail="version required")
        try:
            item = store.save_recommendation(
                deployment_id=auth.settings.deployment_id,
                recommendation_id=recommendation_id,
                expected_version=body.expected_version,
                **_recommendation_fields(body),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        store.record_admin_action(
            actor=principal.audit_actor,
            method="PUT",
            path="recommendation-update",
            status_code=200,
        )
        return item

    @app.post("/api/admin/ops/traces/export")
    def admin_traces_export(
        body: _TraceExportRequest, request: Request
    ) -> Response:
        """批量导出本部署网关记录，含正文时确认部署 ID。"""
        auth.require_admin(request)
        if (
            body.include_content
            and body.confirm_id != auth.settings.deployment_id
        ):
            raise HTTPException(
                status_code=400, detail="deployment confirmation required"
            )
        summaries = store.list_traces(
            deployment_id=auth.settings.deployment_id, limit=100000
        )
        records = (
            store.trace_export(
                trace_id=item["trace_id"],
                include_content=body.include_content,
            )
            for item in summaries
        )
        content = "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in records
            if record is not None
        )
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": (
                    'attachment; filename="weknora-traces.jsonl"'
                ),
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/admin/ops/traces/{trace_id}")
    def admin_trace(trace_id: str, request: Request) -> dict[str, Any]:
        """管理员查看提问人和问答；原生事件正文仍需确认导出。"""
        auth.require_admin(request)
        record = store.trace_overview(
            trace_id=trace_id, deployment_id=auth.settings.deployment_id
        )
        if record is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return record

    @app.post("/api/admin/ops/traces/{trace_id}/review")
    def admin_feedback_review(
        trace_id: str, body: _FeedbackReviewRequest, request: Request
    ) -> dict[str, Any]:
        """复核本候选的真实用户反馈，留存人工状态与备注。"""
        principal = auth.require_admin(request)
        record = store.trace_overview(
            trace_id=trace_id, deployment_id=auth.settings.deployment_id
        )
        if record is None or record["feedback"] is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        store.save_feedback_review(
            trace_id=trace_id,
            status=body.status,
            note=body.note.strip(),
            root_cause=body.root_cause.strip(),
            fix_reference=body.fix_reference.strip(),
            verification_references=[
                item.strip()
                for item in body.verification_references
                if item.strip()
            ],
            evaluation_candidate=body.evaluation_candidate,
        )
        store.record_admin_action(
            actor=principal.audit_actor,
            method="POST",
            path="feedback-review",
            status_code=200,
        )
        updated = store.trace_overview(
            trace_id=trace_id, deployment_id=auth.settings.deployment_id
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="feedback not found")
        return {"review": updated["review"]}

    @app.post("/api/admin/ops/traces/{trace_id}/export")
    def admin_trace_export(
        trace_id: str, body: _TraceExportRequest, request: Request
    ) -> Response:
        """按实际显示内容导出单轮；含正文时确认指定 Trace ID。"""
        auth.require_admin(request)
        if body.include_content and body.confirm_id != trace_id:
            raise HTTPException(
                status_code=400, detail="trace confirmation required"
            )
        record = store.trace_export(
            trace_id=trace_id, include_content=body.include_content
        )
        if (
            record is None
            or record["deployment_id"] != auth.settings.deployment_id
        ):
            raise HTTPException(status_code=404, detail="trace not found")
        return Response(
            content=json.dumps(record, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{trace_id}.json"'
                ),
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/admin/ops/feedback")
    def admin_feedback(request: Request) -> dict[str, Any]:
        """以真实新引擎 Trace 汇总反馈，正文由单条确认导出。"""
        auth.require_admin(request)
        traces = store.list_traces(
            deployment_id=auth.settings.deployment_id,
            limit=1000,
            feedback_only=True,
        )
        return {
            "engine": "weknora",
            "items": traces,
        }

    @app.get("/api/admin/ops/migration")
    def admin_migration(request: Request) -> dict[str, Any]:
        """只读展示与原生知识 ID 对应的原件导入清单。"""
        auth.require_admin(request)
        raw = os.environ.get("WANSHITONG_MIGRATION_MANIFEST_FILE", "")
        if not raw:
            return {"status": "NOT_CONFIGURED", "items": []}
        path = Path(raw)
        if path.is_symlink() or not path.is_file():
            return {"status": "UNAVAILABLE", "items": []}
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise HTTPException(
                status_code=502, detail="migration manifest invalid"
            )
        return {"status": "AVAILABLE", "items": rows}

    @app.get("/api/public/capabilities")
    def capabilities(request: Request) -> dict[str, Any]:
        """向保留的 React 产品壳公布真实网关协议。"""
        auth.require_public(request)
        return {
            "mode": "wanshitong",
            "stream": True,
            "stream_protocol": PROTOCOL,
            "natural_stream_protocol": PROTOCOL,
            "document_visibility": "all_internal",
            "feedback": True,
            "feedback_details": True,
            "request_usage_context": True,
            "shortcuts": [
                {
                    "shortcut_id": item.shortcut_id,
                    "label": item.label,
                    "description": item.description,
                    "revision": item.revision,
                }
                for item in SHORTCUT_CATALOG.public_definitions()
            ],
        }

    @app.get("/api/public/popular-questions")
    def popular_questions(request: Request) -> dict[str, Any]:
        """只发布人工审核过的运营题面，热度来自新引擎真实提问。"""
        auth.require_public(request)
        approved = store.list_recommendations(
            deployment_id=auth.settings.deployment_id, approved_only=True
        )
        frequency = {
            item["question"]: item["count"]
            for item in store.frequent_questions(
                deployment_id=auth.settings.deployment_id, limit=200
            )
        }
        approved.sort(
            key=lambda item: (
                frequency.get(item["question"], 0),
                item["updated_at"],
            ),
            reverse=True,
        )
        return {
            "mode": "POPULAR" if approved else "EMPTY",
            "generated_at": (
                max(item["updated_at"] for item in approved)
                if approved
                else None
            ),
            "window_days": 7,
            "items": [
                {
                    "id": item["recommendation_id"],
                    "question": item["question"],
                    "topic_key": item["topic_key"],
                }
                for item in approved[:20]
            ],
        }

    @app.post("/api/public/chat")
    async def chat(  # noqa: PLR0915
        body: GatewayChatRequest, request: Request
    ) -> StreamingResponse:
        """以原文向固定 KB 发起一次原生问答并逐帧转换。"""
        principal = auth.require_public(request)
        user_id = principal.user_id
        conversation_id = body.conversation_id
        if user_id is None:
            raise HTTPException(status_code=401, detail="RDMS login required")
        owner_id = _owner_id(auth, user_id)
        client_context = _usage_context(body.client_context)
        retry_of = client_context.get("retry_of_trace_id")
        if retry_of is not None:
            original = store.get_turn(trace_id=retry_of, owner_id=owner_id)
            if (
                original is None
                or original["conversation_id"] != conversation_id
            ):
                raise HTTPException(
                    status_code=404, detail="original turn not found"
                )
        lock = locks.setdefault((owner_id, conversation_id), asyncio.Lock())
        if lock.locked():
            raise HTTPException(status_code=409, detail="conversation busy")
        await lock.acquire()
        try:
            native_session_id = store.native_session(
                deployment_id=auth.settings.deployment_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
            )
            if native_session_id is None:
                created = await native.create_session(user_id)
                native_session_id = store.bind_session(
                    deployment_id=auth.settings.deployment_id,
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    native_session_id=created,
                )
            trace_id = "trace_" + secrets.token_hex(16)
            store.start_turn(
                trace_id=trace_id,
                deployment_id=auth.settings.deployment_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                native_session_id=native_session_id,
                question=body.query,
                asker_name=(
                    getattr(principal, "nick_name", None)
                    or getattr(principal, "username", None)
                ),
                client_context=client_context,
                kb_scope=engine.public_kb_ids,
            )
        except NativeHttpError as error:
            lock.release()
            raise HTTPException(
                status_code=502, detail=f"native status {error.status_code}"
            ) from None
        except Exception:
            lock.release()
            raise

        bridge = BridgeStream(
            trace_id=trace_id,
            session_id=native_session_id,
            reference_mapper=_reference_mapper(store, trace_id),
        )

        async def generate() -> AsyncIterator[bytes]:  # noqa: PLR0912, PLR0915 - 单次流的终态统一落库。
            status = "streaming"
            try:
                meta = bridge.meta()
                _record_frame(store, trace_id, meta)
                yield meta
                decoder = NativeSseDecoder()
                async for chunk in native.knowledge_chat(
                    user_id=user_id,
                    session_id=native_session_id,
                    question=body.query,
                    knowledge_base_ids=engine.public_kb_ids,
                ):
                    for event in decoder.feed(chunk):
                        for frame in bridge.accept(event):
                            if bridge.message_id is not None:
                                store.set_native_message(
                                    trace_id=trace_id,
                                    native_message_id=bridge.message_id,
                                    native_request_id=bridge.request_id,
                                )
                            _record_frame(store, trace_id, frame)
                            yield frame
                        if bridge.terminal:
                            break
                    if bridge.terminal:
                        break
                if not bridge.terminal:
                    for event in decoder.finish():
                        for frame in bridge.accept(event):
                            _record_frame(store, trace_id, frame)
                            yield frame
                if not bridge.terminal:
                    disconnect_frame = bridge.disconnected()
                    if disconnect_frame is not None:
                        _record_frame(store, trace_id, disconnect_frame)
                        yield disconnect_frame
                status = (
                    "completed"
                    if bridge.terminal_type == "complete"
                    else "cancelled"
                    if bridge.terminal_type == "stopped"
                    else "failed"
                )
                if bridge.error_category:
                    store.record_diagnostic(
                        trace_id=trace_id,
                        category=bridge.error_category,
                        phase="native_stream",
                    )
            except asyncio.CancelledError:
                status = "disconnected"
                store.record_diagnostic(
                    trace_id=trace_id,
                    category="CLIENT_DISCONNECTED",
                    phase="downstream_stream",
                )
                raise
            except NativeHttpError as error:
                status = "failed"
                store.record_diagnostic(
                    trace_id=trace_id,
                    category=error.category,
                    phase=error.phase,
                    upstream_status=error.status_code,
                )
                error_frame = bridge.disconnected(code=error.category)
                if error_frame is not None:
                    _record_frame(store, trace_id, error_frame)
                    yield error_frame
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                ValueError,
                UnicodeError,
            ) as error:
                status = "failed"
                if isinstance(error, httpx.TimeoutException):
                    category = "UPSTREAM_TIMEOUT"
                elif isinstance(error, httpx.RemoteProtocolError):
                    category = "UPSTREAM_PROTOCOL_ERROR"
                elif isinstance(error, httpx.TransportError):
                    category = "UPSTREAM_CONNECTION_ERROR"
                else:
                    category = "INVALID_NATIVE_STREAM"
                store.record_diagnostic(
                    trace_id=trace_id, category=category, phase="native_stream"
                )
                error_frame = bridge.disconnected(code=category)
                if error_frame is not None:
                    _record_frame(store, trace_id, error_frame)
                    yield error_frame
            except Exception:
                status = "failed"
                store.record_diagnostic(
                    trace_id=trace_id,
                    category="GATEWAY_STREAM_ERROR",
                    phase="gateway_stream",
                )
                error_frame = bridge.disconnected(code="GATEWAY_STREAM_ERROR")
                if error_frame is not None:
                    _record_frame(store, trace_id, error_frame)
                    yield error_frame
            finally:
                try:
                    store.finish_turn(
                        trace_id=trace_id,
                        status=status,
                        answer=bridge.answer,
                        references=bridge.references,
                        native_message_id=bridge.message_id,
                        native_request_id=bridge.request_id,
                        truncated=bridge.truncated,
                        finish_reason=bridge.finish_reason,
                    )
                finally:
                    lock.release()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )

    @app.post("/api/public/chat/{trace_id}/continue")
    async def continue_chat(  # noqa: PLR0915 - 续流认证、映射和关闭保持同一事务边界。
        trace_id: str, body: PublicChatStopRequest, request: Request
    ) -> StreamingResponse:
        """只重放并续接同一原生消息；不产生第二次知识问答 POST。"""
        principal = auth.require_public(request)
        user_id = _required_user(principal.user_id)
        owner_id = _owner_id(auth, user_id)
        turn = store.get_turn(trace_id=trace_id, owner_id=owner_id)
        if turn is None or turn["conversation_id"] != body.conversation_id:
            raise HTTPException(status_code=404, detail="turn not found")
        message_id = turn["native_message_id"]
        if not isinstance(message_id, str) or not message_id:
            raise HTTPException(
                status_code=409,
                detail="native message not available for recovery",
            )
        lock = locks.setdefault(
            (owner_id, body.conversation_id), asyncio.Lock()
        )
        if lock.locked():
            raise HTTPException(status_code=409, detail="conversation busy")
        await lock.acquire()
        try:
            store.mark_recovering(trace_id=trace_id, owner_id=owner_id)
        except Exception:
            lock.release()
            raise
        bridge = BridgeStream(
            trace_id=trace_id,
            session_id=str(turn["native_session_id"]),
            reference_mapper=_reference_mapper(store, trace_id),
        )

        async def generate_recovery() -> AsyncIterator[bytes]:  # noqa: PLR0912 - 按原生终态封存同一轮。
            status = "disconnected"
            try:
                meta = bridge.meta()
                _record_recovery_frame(store, trace_id, meta)
                yield meta
                decoder = NativeSseDecoder()
                async for chunk in native.continue_stream(
                    user_id=user_id,
                    session_id=str(turn["native_session_id"]),
                    message_id=message_id,
                ):
                    for event in decoder.feed(chunk):
                        for frame in bridge.accept(event):
                            _record_recovery_frame(store, trace_id, frame)
                            yield frame
                        if bridge.terminal:
                            break
                    if bridge.terminal:
                        break
                if not bridge.terminal:
                    for event in decoder.finish():
                        for frame in bridge.accept(event):
                            _record_recovery_frame(store, trace_id, frame)
                            yield frame
                if not bridge.terminal:
                    disconnect_frame = bridge.disconnected()
                    if disconnect_frame is not None:
                        _record_recovery_frame(
                            store, trace_id, disconnect_frame
                        )
                        yield disconnect_frame
                status = (
                    "completed"
                    if bridge.terminal_type == "complete"
                    else "cancelled"
                    if bridge.terminal_type == "stopped"
                    else "failed"
                )
                if bridge.error_category:
                    store.record_diagnostic(
                        trace_id=trace_id,
                        category=bridge.error_category,
                        phase="native_continue_stream",
                    )
            except asyncio.CancelledError:
                status = "disconnected"
                raise
            except (
                NativeHttpError,
                httpx.TransportError,
                ValueError,
                UnicodeError,
            ) as error:
                status = "failed"
                category = (
                    error.category
                    if isinstance(error, NativeHttpError)
                    else "RECOVERY_STREAM_ERROR"
                )
                store.record_diagnostic(
                    trace_id=trace_id,
                    category=category,
                    phase="native_continue_stream",
                    upstream_status=error.status_code
                    if isinstance(error, NativeHttpError)
                    else None,
                )
                error_frame = bridge.disconnected(code=category)
                if error_frame is not None:
                    _record_recovery_frame(store, trace_id, error_frame)
                    yield error_frame
            finally:
                try:
                    store.finish_turn(
                        trace_id=trace_id,
                        status=status,
                        answer=bridge.answer or str(turn["answer"] or ""),
                        references=bridge.references
                        or tuple(json.loads(turn["references_json"])),
                        native_message_id=bridge.message_id or message_id,
                        native_request_id=bridge.request_id
                        or turn["native_request_id"],
                        truncated=bridge.truncated or bool(turn["truncated"]),
                        finish_reason=bridge.finish_reason
                        or turn["finish_reason"],
                    )
                finally:
                    lock.release()

        return StreamingResponse(
            generate_recovery(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )

    @app.post("/api/public/chat/{trace_id}/stop")
    async def stop(
        trace_id: str, body: PublicChatStopRequest, request: Request
    ) -> dict[str, bool]:
        """校验归属后向原生停止当前助手消息。"""
        principal = auth.require_public(request)
        user_id = _required_user(principal.user_id)
        owner_id = _owner_id(auth, user_id)
        turn = store.get_turn(trace_id=trace_id, owner_id=owner_id)
        if turn is None or turn["conversation_id"] != body.conversation_id:
            raise HTTPException(status_code=404, detail="turn not found")
        message_id = turn["native_message_id"]
        if not isinstance(message_id, str) or not message_id:
            return {"cancelled": False}
        try:
            cancelled = await native.stop(
                user_id=user_id,
                session_id=str(turn["native_session_id"]),
                message_id=message_id,
            )
        except NativeHttpError:
            raise HTTPException(
                status_code=502, detail="native stop failed"
            ) from None
        if cancelled:
            store.request_stop(trace_id=trace_id, owner_id=owner_id)
        return {"cancelled": cancelled}

    @app.get("/api/public/conversations")
    async def conversations(request: Request) -> dict[str, Any]:
        """只列本人的新原生会话，并读取原生当前标题。"""
        principal = auth.require_public(request, csrf=True)
        user_id = _required_user(principal.user_id)
        owner_id = _owner_id(auth, user_id)
        rows = store.list_conversations(
            deployment_id=auth.settings.deployment_id,
            owner_id=owner_id,
        )
        items = []
        for row in rows[:_MAX_HISTORY_SESSIONS]:
            try:
                session = await native.session(
                    user_id=user_id,
                    session_id=row["native_session_id"],
                )
            except NativeHttpError:
                continue
            items.append(
                {
                    "conversation_id": row["conversation_id"],
                    "title": session.get("title") or "新对话",
                    "updated_at": row["updated_at"],
                }
            )
        return {
            "items": items,
            "total": len(rows),
            "limit": _MAX_HISTORY_SESSIONS,
            "has_more": len(rows) > _MAX_HISTORY_SESSIONS,
        }

    @app.get("/api/public/legacy/history")
    def legacy_history(request: Request) -> dict[str, Any]:
        """按当前 RDMS 身份只读列出切换前记录。"""
        principal = auth.require_public(request, csrf=True)
        if legacy is None:
            return {"items": [], "status": "NOT_CONFIGURED"}
        return {
            "items": legacy.list_for_user(_required_user(principal.user_id)),
            "status": "AVAILABLE",
        }

    @app.get("/api/public/legacy/history/{trace_id}")
    def legacy_history_detail(
        trace_id: str, request: Request
    ) -> dict[str, Any]:
        """旧答案只读展示，不提供续聊或新引擎引用。"""
        principal = auth.require_public(request, csrf=True)
        if legacy is None:
            raise HTTPException(
                status_code=404, detail="legacy history unavailable"
            )
        record = legacy.get_for_user(
            _required_user(principal.user_id), trace_id
        )
        if record is None:
            raise HTTPException(
                status_code=404, detail="legacy history not found"
            )
        return record

    @app.get("/api/public/conversations/{conversation_id}")
    async def conversation_history(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        """从原生消息读回正文，本地仅保留归属与 Trace 映射。"""
        principal = auth.require_public(request, csrf=True)
        user_id = _required_user(principal.user_id)
        owner_id = _owner_id(auth, user_id)
        native_session_id = store.native_session(
            deployment_id=auth.settings.deployment_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
        if native_session_id is None:
            return {"turns": []}
        try:
            messages = await native.messages(
                user_id=user_id, session_id=native_session_id
            )
        except NativeHttpError:
            raise HTTPException(
                status_code=502, detail="native history unavailable"
            ) from None
        native_by_id = {
            item.get("id"): item
            for item in messages
            if isinstance(item.get("id"), str)
        }
        turns = []
        for turn in store.list_turns(
            deployment_id=auth.settings.deployment_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
        ):
            native_message = native_by_id.get(turn["native_message_id"])
            native_answer = (
                native_message.get("content")
                if isinstance(native_message, dict)
                else None
            )
            status = str(turn["status"])
            source_status = (
                "available" if native_message is not None else "missing"
            )
            if native_message is None and not turn["native_message_id"]:
                source_status = "not_created"
            if (
                native_message is None
                and turn["native_message_id"]
                and status == "completed"
            ):
                status = "SOURCE_UNAVAILABLE"
            elif native_message is not None:
                is_completed = native_message.get("is_completed")
                if is_completed is True and status in {
                    "streaming",
                    "disconnected",
                    "stop_requested",
                }:
                    # 原生 completed 只说明消息已终止，不能证明生成成功。
                    status = "pending_confirmation"
                elif is_completed is False and status == "completed":
                    status = "pending_confirmation"
            if status == "streaming" and native_message is None:
                status = "pending_confirmation"
            answer = (
                native_answer
                if isinstance(native_answer, str)
                else turn["answer"]
            )
            if status == "SOURCE_UNAVAILABLE":
                answer = None
            turns.append(
                {
                    "turn_id": turn["trace_id"],
                    "trace_id": turn["trace_id"],
                    "question": turn["question"],
                    "status": status,
                    "source_status": source_status,
                    "native_is_completed": native_message.get("is_completed")
                    if native_message is not None
                    else None,
                    "answer": answer if isinstance(answer, str) else None,
                    "citations": json.loads(turn["references_json"]),
                    "partial": status
                    in {
                        "failed",
                        "disconnected",
                        "stop_requested",
                        "pending_confirmation",
                    }
                    and bool(answer),
                    "truncated": bool(turn["truncated"]),
                    "finish_reason": turn["finish_reason"],
                    "native_message_id": turn["native_message_id"],
                    "native_request_id": turn["native_request_id"],
                    "stop_acknowledged": bool(turn["stop_acknowledged"]),
                    "created_at": turn["created_at"],
                }
            )
        return {"turns": turns}

    @app.get(
        "/api/public/conversations/{conversation_id}/turns/"
        "{trace_id}/references/{ref_id}/source"
    )
    async def reference_source(
        conversation_id: str,
        trace_id: str,
        ref_id: str,
        request: Request,
    ) -> Response:
        """用原生消息级接口读取已证实属于本人的资源句柄。"""
        principal = auth.require_public(request, csrf=True)
        user_id = _required_user(principal.user_id)
        turn = store.get_turn(
            trace_id=trace_id, owner_id=_owner_id(auth, user_id)
        )
        if turn is None or turn["conversation_id"] != conversation_id:
            raise HTTPException(status_code=404, detail="turn not found")
        reference = store.get_reference(reference_id=ref_id, trace_id=trace_id)
        if reference is None:
            raise HTTPException(status_code=404, detail="resource unavailable")
        if not reference["resource_handle"]:
            native_reference = json.loads(str(reference["source_json"]))
            excerpt = native_reference.get("content")
            if not isinstance(excerpt, str) or not excerpt:
                raise HTTPException(
                    status_code=404, detail="resource unavailable"
                )
            return Response(
                content=excerpt,
                media_type="text/plain; charset=utf-8",
                headers={"Cache-Control": "private, no-store"},
            )
        message_id = turn["native_message_id"]
        if not isinstance(message_id, str) or not message_id:
            raise HTTPException(status_code=409, detail="message not ready")
        try:
            upstream = await native.message_resource(
                user_id=user_id,
                session_id=str(turn["native_session_id"]),
                message_id=message_id,
                file_path=str(reference["resource_handle"]),
            )
        except NativeHttpError:
            raise HTTPException(
                status_code=404, detail="resource unavailable"
            ) from None
        if len(upstream.content) > _MAX_RESOURCE_BYTES:
            raise HTTPException(status_code=413, detail="resource too large")
        media_type = upstream.headers.get(
            "content-type", "application/octet-stream"
        )
        return Response(
            content=upstream.content,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get(
        "/api/public/conversations/{conversation_id}/turns/"
        "{trace_id}/references/{ref_id}/original"
    )
    async def reference_original(
        conversation_id: str,
        trace_id: str,
        ref_id: str,
        request: Request,
    ) -> Response:
        """只允许下载本人实际答案引用过的原生原件。"""
        principal = auth.require_public(request, csrf=True)
        turn = store.get_turn(
            trace_id=trace_id,
            owner_id=_owner_id(auth, _required_user(principal.user_id)),
        )
        if turn is None or turn["conversation_id"] != conversation_id:
            raise HTTPException(status_code=404, detail="turn not found")
        reference = store.get_reference(reference_id=ref_id, trace_id=trace_id)
        if reference is None:
            raise HTTPException(status_code=404, detail="resource unavailable")
        try:
            knowledge_id = str(UUID(str(reference["native_knowledge_id"])))
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(
                status_code=404, detail="original unavailable"
            ) from None
        upstream = await admin_native.proxy(
            method="GET",
            path=f"api/v1/knowledge/{knowledge_id}/download",
            query="",
            content=b"",
            browser_headers={},
        )
        if upstream.status_code != _HTTP_OK:
            raise HTTPException(status_code=404, detail="original unavailable")
        if len(upstream.content) > _MAX_RESOURCE_BYTES:
            raise HTTPException(status_code=413, detail="original too large")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                **upstream.headers,
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/api/public/conversations/{conversation_id}/turns/{trace_id}/resources"
    )
    async def answer_image(
        conversation_id: str,
        trace_id: str,
        request: Request,
        file_path: str,
    ) -> Response:
        """原生消息级资源鉴权后才允许渲染答案中的图片。"""
        principal = auth.require_public(request, csrf=False)
        user_id = _required_user(principal.user_id)
        turn = store.get_turn(
            trace_id=trace_id, owner_id=_owner_id(auth, user_id)
        )
        if turn is None or turn["conversation_id"] != conversation_id:
            raise HTTPException(status_code=404, detail="turn not found")
        if (
            not file_path.startswith("resource://")
            or len(file_path) > _MAX_RESOURCE_HANDLE_CHARS
        ):
            raise HTTPException(status_code=400, detail="invalid resource")
        message_id = turn["native_message_id"]
        if not isinstance(message_id, str) or not message_id:
            raise HTTPException(status_code=409, detail="message not ready")
        try:
            upstream = await native.message_resource(
                user_id=user_id,
                session_id=str(turn["native_session_id"]),
                message_id=message_id,
                file_path=file_path,
            )
        except NativeHttpError:
            raise HTTPException(
                status_code=404, detail="resource unavailable"
            ) from None
        media_type = upstream.headers.get("content-type", "").split(";", 1)[0]
        if media_type.lower() not in _INLINE_IMAGE_TYPES:
            raise HTTPException(status_code=415, detail="unsupported image")
        if len(upstream.content) > _MAX_RESOURCE_BYTES:
            raise HTTPException(status_code=413, detail="resource too large")
        return Response(
            content=upstream.content,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/public/feedback")
    def feedback(
        body: PublicFeedbackRequest, request: Request
    ) -> dict[str, Any]:
        """反馈仅关联本用户的真实新引擎 Trace。"""
        principal = auth.require_public(request)
        user_id = _required_user(principal.user_id)
        owner_id = _owner_id(auth, user_id)
        turn = store.get_turn(trace_id=body.trace_id, owner_id=owner_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="turn not found")
        try:
            store.save_feedback(
                trace_id=body.trace_id,
                owner_id=owner_id,
                useful=body.useful,
                reason_code=body.reason_code if body.reason_code else None,
                reason_detail=body.reason_detail
                if body.reason_detail
                else None,
                comment=body.comment,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409, detail="feedback already submitted"
            ) from None
        return {
            "trace_id": body.trace_id,
            "useful": body.useful,
            "engine": "weknora",
            "native_message_id": turn["native_message_id"],
        }

    return app


def _required_user(user_id: str | None) -> str:
    if user_id is None:
        raise HTTPException(status_code=401, detail="RDMS login required")
    return user_id


def _usage_context(value: object | None) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    entrypoint = value.get("entrypoint")
    if entrypoint in {"manual", "suggestion", "popular", "retry"}:
        result["entrypoint"] = entrypoint
    recommendation_id = value.get("recommendation_id")
    if (
        isinstance(recommendation_id, str)
        and len(recommendation_id) <= _MAX_RECOMMENDATION_ID_LENGTH
    ):
        result["recommendation_id"] = recommendation_id
    retry_of = value.get("retry_of_trace_id")
    if retry_of is not None:
        if (
            not isinstance(retry_of, str)
            or re.fullmatch(r"trace_[0-9a-f]{32}", retry_of) is None
        ):
            raise HTTPException(
                status_code=422, detail="invalid original trace"
            )
        result["retry_of_trace_id"] = retry_of
    return result


def _owner_id(auth: GatewayAuth, user_id: str) -> str:
    return f"rdms:{auth.settings.deployment_id}:{user_id}"


def _reference_mapper(
    store: GatewayStore, trace_id: str
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    index = 0

    def map_reference(native: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal index
        current_id = reference_id(trace_id, index, native)
        index += 1
        store.add_reference(
            reference_id=current_id,
            trace_id=trace_id,
            source=native,
            resource_handle=resource_handle(native),
        )
        return public_citation(native, reference_id=current_id)

    return map_reference


def _record_frame(store: GatewayStore, trace_id: str, frame: bytes) -> None:
    event_line, data_line, *_rest = frame.decode().splitlines()
    event_type = event_line.removeprefix("event: ")
    payload = json.loads(data_line.removeprefix("data: "))
    store.record_event(
        trace_id=trace_id,
        sequence=payload["sequence"],
        event_type=event_type,
        payload=payload,
    )


def _record_recovery_frame(
    store: GatewayStore, trace_id: str, frame: bytes
) -> None:
    event_line, data_line, *_rest = frame.decode().splitlines()
    event_type = event_line.removeprefix("event: ")
    payload = json.loads(data_line.removeprefix("data: "))
    store.append_recovery_event(
        trace_id=trace_id, event_type=event_type, payload=payload
    )
