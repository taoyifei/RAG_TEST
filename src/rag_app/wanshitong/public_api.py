"""湾事通无登录公共 API，只委托既有 Universal 产品能力。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from rag_app.api.p09_schemas import QueryRequest
from rag_app.api.p09_stream import P09AnswerStream, P09AnswerStreamRequest
from rag_app.application.answering.natural_answer import NaturalReference
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import NotFound, PolicyDenied, RagError
from rag_app.core.identifiers import new_id
from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.product.conversations import natural_reference_id
from rag_app.product.http_security import secure_cookie_for_request
from rag_app.query_executor import QueryAdmissionError
from rag_app.tracing import TraceMode
from rag_app.wanshitong.natural_stream import (
    NATURAL_PUBLIC_PROTOCOL,
    NaturalPublicStream,
    NaturalStreamRegistry,
    public_natural_references,
)
from rag_app.wanshitong.public_models import (
    PublicCapabilities,
    PublicChatRequest,
    PublicChatStopRequest,
    PublicConversationClearResponse,
    PublicFeedbackRequest,
    PublicFeedbackResponse,
    PublicPopularQuestions,
    PublicSessionRequest,
    PublicSessionResponse,
    PublicSessionUser,
    PublicShortcut,
)
from rag_app.wanshitong.public_session import (
    PublicSessionPrincipal,
    PublicSessionProvider,
)
from rag_app.wanshitong.public_stream import (
    project_public_stream,
    render_public_final,
)
from rag_app.wanshitong.question_recommendations import (
    QuestionRecommendationService,
)
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.shortcuts import (
    SHORTCUT_CATALOG,
    current_public_filter,
)
from rag_app.wanshitong.usage_hints import parse_client_context

PUBLIC_SESSION_PATH = "/api/public/session"
PUBLIC_CAPABILITIES_PATH = "/api/public/capabilities"
PUBLIC_CHAT_PATH = "/api/public/chat"
PUBLIC_CHAT_STOP_PATH = "/api/public/chat/{trace_id}/stop"
PUBLIC_FEEDBACK_PATH = "/api/public/feedback"
PUBLIC_POPULAR_QUESTIONS_PATH = "/api/public/popular-questions"
PUBLIC_CONVERSATIONS_PATH = "/api/public/conversations"
PUBLIC_CONVERSATION_PATH = "/api/public/conversations/{conversation_id}"
_PUBLIC_STREAM_FIRST_CONTENT_SECONDS = 120.0
_PUBLIC_STREAM_IDLE_SECONDS = 120.0
_PUBLIC_STREAM_TOTAL_SECONDS = 180.0


class _PublicStreamingResponse(StreamingResponse):
    """在任意 HTTP 结束路径上取消既有 P09 协调器。"""

    def __init__(
        self,
        content: Iterator[bytes],
        *,
        cancel: Callable[[], None],
        headers: dict[str, str],
    ) -> None:
        super().__init__(
            content, media_type="text/event-stream", headers=headers
        )
        self._cancel_stream = cancel

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """传播响应完成、发送失败或客户端断连。"""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cancel_stream()


def register_public_routes(  # noqa: PLR0913, PLR0915
    app: FastAPI,
    *,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
    sessions: PublicSessionProvider,
    recommendations: QuestionRecommendationService,
    natural_public_enabled: bool = False,
    natural_public_engine: Literal["wk-standard-v1", "wk-standard-pc-v1"] = (
        "wk-standard-pc-v1"
    ),
) -> None:
    """注册固定 Scope 的匿名 Facade，不新增查询或存储服务。

    Args:
        app: 已安装 Product 安全中间件的 FastAPI 应用。
        runtime: 唯一 Universal Product Runtime。
        scope_service: WB-01 已校验的固定 Scope 服务。
        sessions: 由部署主密钥派生的匿名会话服务。
        recommendations: 已审核公共题目录。
        natural_public_enabled: 仅候选部署启用的自然流开关。
        natural_public_engine: 服务器固定的自然问答引擎。

    """
    natural_streams = NaturalStreamRegistry()

    @app.post(
        PUBLIC_SESSION_PATH,
        tags=["wanshitong-public"],
        response_model=PublicSessionResponse,
        response_model_exclude_none=True,
    )
    def _session(
        request: Request,
        response: Response,
        body: PublicSessionRequest | None = None,
    ) -> PublicSessionResponse:
        del body
        _reject_query_parameters(request)
        try:
            issue = sessions.bootstrap(
                request.cookies.get(sessions.cookie_name)
            )
        except PolicyDenied:
            raise HTTPException(
                status_code=401, detail="public login required"
            ) from None
        if sessions.set_cookie_on_bootstrap:
            response.set_cookie(
                sessions.cookie_name,
                issue.cookie_value,
                httponly=True,
                secure=secure_cookie_for_request(
                    request,
                    trusted_proxies=runtime.settings.trusted_proxies,
                ),
                samesite=sessions.cookie_samesite,
                max_age=issue.expires_in,
                path=sessions.cookie_path,
            )
        user = None
        if issue.principal.user_id is not None:
            user = PublicSessionUser(
                user_id=issue.principal.user_id,
                display_name=(
                    issue.principal.nick_name
                    or issue.principal.username
                    or issue.principal.user_id
                ),
            )
        return PublicSessionResponse(
            session_id=issue.principal.session_id,
            csrf_token=issue.csrf_token,
            expires_in=issue.expires_in,
            user=user,
            deployment_id=sessions.deployment_id,
        )

    @app.get(
        PUBLIC_CAPABILITIES_PATH,
        tags=["wanshitong-public"],
        response_model=PublicCapabilities,
        response_model_exclude_none=True,
    )
    def _capabilities(request: Request) -> PublicCapabilities:
        _reject_query_parameters(request)
        _authenticate_public_cookie(request, sessions)
        scope_service.binding()
        return PublicCapabilities(
            natural_stream_protocol=(
                NATURAL_PUBLIC_PROTOCOL if natural_public_enabled else None
            ),
            shortcuts=tuple(
                PublicShortcut(
                    shortcut_id=item.shortcut_id,
                    label=item.label,
                    description=item.description,
                    revision=item.revision,
                )
                for item in SHORTCUT_CATALOG.public_definitions()
            ),
        )

    @app.get(
        PUBLIC_POPULAR_QUESTIONS_PATH,
        tags=["wanshitong-public"],
        response_model=PublicPopularQuestions,
    )
    def _popular_questions(request: Request) -> PublicPopularQuestions:
        _reject_query_parameters(request)
        _authenticate_public_cookie(request, sessions)
        binding = scope_service.binding()
        return PublicPopularQuestions.model_validate(
            recommendations.public_questions(
                project_id=binding.project_id,
                knowledge_base_id=binding.knowledge_base_id,
            )
        )

    @app.post(
        PUBLIC_CHAT_PATH,
        tags=["wanshitong-public"],
        response_model=None,
    )
    def _chat(
        body: PublicChatRequest,
        request: Request,
    ) -> StreamingResponse:
        _reject_query_parameters(request)
        principal, cookie_value = _authenticate_public_request(
            request, sessions
        )
        binding = scope_service.binding()
        metadata_filter = current_public_filter()
        if not metadata_filter.is_empty:
            raise AssertionError("WB-06 公共查询必须覆盖为空 metadata filter。")
        query = QueryRequest(
            query=body.query,
            conversation_id=body.conversation_id,
            limit=10,
            stream=True,
            stream_protocol="rag-answer-sse-v1",
            include_related_content=False,
            history_mode="full",
            trace_mode="SAFE",
        )
        trace_id = new_id("trace")
        hint = parse_client_context(body.client_context)
        audit_context = QueryAuditContext(
            trace_id=trace_id,
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            owner_id=principal.owner_id,
            deployment_id=sessions.deployment_id,
            identity_source=(
                "RDMS_SSO"
                if principal.user_id is not None
                else "ANONYMOUS_SESSION"
            ),
            traffic_class="INTERACTIVE",
            classification_source="PUBLIC_ENDPOINT",
            entrypoint=hint.entrypoint,
            entrypoint_source=hint.source,
            recommendation_id=hint.recommendation_id,
            hint_diagnostic=hint.diagnostic,
        )
        if runtime.p09.prepare_trace is not None:
            runtime.p09.prepare_trace(trace_id, TraceMode.SAFE)
        runtime.sdk.require_active_knowledge_base(
            binding.project_id, binding.knowledge_base_id
        )
        requested_protocol = request.headers.get("X-Wanshitong-Stream-Protocol")
        if requested_protocol is not None:
            if requested_protocol != NATURAL_PUBLIC_PROTOCOL:
                raise HTTPException(
                    status_code=406, detail="unsupported stream protocol"
                )
            if not natural_public_enabled:
                raise HTTPException(
                    status_code=409, detail="natural stream disabled"
                )
            if body.conversation_id is None:
                raise HTTPException(
                    status_code=422, detail="conversation_id required"
                )
            natural = NaturalPublicStream(
                runtime=runtime,
                executor=runtime.p09.query_executor,
                scope=KnowledgeBaseScope(
                    project_id=binding.project_id,
                    knowledge_base_id=binding.knowledge_base_id,
                ),
                question=body.query,
                conversation_id=body.conversation_id,
                owner_id=principal.owner_id,
                trace_id=trace_id,
                engine_id=natural_public_engine,
                audit_context=audit_context,
                authorization_guard=lambda: _validate_stream_session(
                    sessions, cookie_value, principal
                ),
            )
            try:
                natural_iterator = natural.start()
            except QueryAdmissionError as error:
                raise RagError(
                    "查询容量已满，请稍后重试。",
                    stage="query.admission",
                    code="QUEUE_LIMIT_EXCEEDED",
                    retryable=True,
                    trace_id=trace_id,
                ) from error
            natural_streams.register(natural)

            def cleanup_natural_stream() -> None:
                try:
                    natural.cancel()
                finally:
                    natural_streams.discard(natural)

            return _PublicStreamingResponse(
                natural_iterator,
                cancel=cleanup_natural_stream,
                headers={
                    "Cache-Control": "no-store, no-transform",
                    "X-Accel-Buffering": "no",
                    "X-Trace-Id": trace_id,
                },
            )
        stream = P09AnswerStream(
            executor=runtime.p09.query_executor,
            sdk=runtime.sdk,
            request=P09AnswerStreamRequest(
                project_id=binding.project_id,
                knowledge_base_id=binding.knowledge_base_id,
                question=query.query,
                trace_id=trace_id,
                limit=query.limit,
                include_related_content=query.include_related_content,
                history_mode="full",
                owner_id=principal.owner_id,
                conversation_id=query.conversation_id,
                audit_context=audit_context,
            ),
            render_final=render_public_final,
            versioned_protocol=True,
            # 内网演示模型需要在首个事实前完成整份结构化回答及一次修复；
            # 四并发实测一次修复可能超过 60 秒。只放宽湾事通公共壳层，
            # 不改变 Universal 默认门禁。
            first_content_seconds=_PUBLIC_STREAM_FIRST_CONTENT_SECONDS,
            idle_seconds=_PUBLIC_STREAM_IDLE_SECONDS,
            total_seconds=_PUBLIC_STREAM_TOTAL_SECONDS,
            authorization_guard=lambda: _validate_stream_session(
                sessions, cookie_value, principal
            ),
        )
        try:
            iterator = stream.start()
        except QueryAdmissionError as error:
            raise RagError(
                "查询容量已满，请稍后重试。",
                stage="query.admission",
                code="QUEUE_LIMIT_EXCEEDED",
                retryable=True,
                trace_id=trace_id,
                details={
                    "retry_after_seconds": (
                        runtime.p09.query_executor.retry_after_seconds
                    ),
                    "admission_reason": type(error).__name__,
                },
            ) from error
        return _PublicStreamingResponse(
            project_public_stream(iterator),
            cancel=stream.cancel,
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )

    @app.post(PUBLIC_CHAT_STOP_PATH, tags=["wanshitong-public"])
    def _stop_natural_chat(
        body: PublicChatStopRequest,
        request: Request,
        trace_id: Annotated[str, Path(pattern=r"^trace_[0-9a-f]{32}$")],
    ) -> dict[str, bool]:
        _reject_query_parameters(request)
        if not natural_public_enabled:
            raise HTTPException(
                status_code=404, detail="natural stream disabled"
            )
        principal, _ = _authenticate_public_request(request, sessions)
        return {
            "cancelled": natural_streams.cancel_owned(
                trace_id,
                owner_id=principal.owner_id,
                conversation_id=body.conversation_id,
            )
        }

    @app.get(PUBLIC_CONVERSATIONS_PATH, tags=["wanshitong-public"])
    def _natural_sessions(request: Request) -> dict[str, object]:
        _reject_query_parameters(request)
        if not natural_public_enabled:
            raise HTTPException(
                status_code=404, detail="natural history disabled"
            )
        principal, _ = _authenticate_public_request(request, sessions)
        binding = scope_service.binding()
        scope = KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        items = runtime.conversations.natural_sessions(
            scope, owner_id=principal.owner_id
        )
        return {
            "items": [
                {
                    "conversation_id": item.conversation_id,
                    "title": item.title,
                    "updated_at": item.updated_at,
                }
                for item in items
            ]
        }

    @app.get(
        PUBLIC_CONVERSATION_PATH,
        tags=["wanshitong-public"],
    )
    def _natural_history(
        conversation_id: Annotated[
            str,
            Path(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            ),
        ],
        request: Request,
    ) -> dict[str, object]:
        _reject_query_parameters(request)
        if not natural_public_enabled:
            raise HTTPException(
                status_code=404, detail="natural history disabled"
            )
        principal, _ = _authenticate_public_request(request, sessions)
        binding = scope_service.binding()
        scope = KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        records = runtime.conversations.natural_turns(
            scope, conversation_id, owner_id=principal.owner_id
        )
        return {
            "conversation_id": conversation_id,
            "turns": [
                {
                    "turn_id": item.trace_id,
                    "trace_id": item.trace_id,
                    "question": item.question,
                    "status": item.status,
                    "answer": item.answer,
                    "citation_status": item.citation_status,
                    "validation_level": item.validation_level,
                    "citations": public_natural_references(
                        item.trace_id, item.references
                    ),
                    "created_at": item.created_at,
                }
                for item in records
            ],
        }

    @app.get(
        PUBLIC_CONVERSATION_PATH
        + "/turns/{trace_id}/references/{reference_id}",
        tags=["wanshitong-public"],
    )
    def _natural_reference(
        conversation_id: str,
        trace_id: Annotated[str, Path(pattern=r"^trace_[0-9a-f]{32}$")],
        reference_id: Annotated[str, Path(pattern=r"^ref_[0-9a-f]{32}$")],
        request: Request,
    ) -> dict[str, object]:
        _reject_query_parameters(request)
        _reference, citation = _resolve_natural_reference(
            request, conversation_id, trace_id, reference_id
        )
        return citation

    @app.get(
        PUBLIC_CONVERSATION_PATH
        + "/turns/{trace_id}/references/{reference_id}/source",
        tags=["wanshitong-public"],
    )
    def _natural_source(
        conversation_id: str,
        trace_id: Annotated[str, Path(pattern=r"^trace_[0-9a-f]{32}$")],
        reference_id: Annotated[str, Path(pattern=r"^ref_[0-9a-f]{32}$")],
        request: Request,
    ) -> Response:
        _reject_query_parameters(request)
        reference, _citation = _resolve_natural_reference(
            request, conversation_id, trace_id, reference_id
        )
        binding = scope_service.binding()
        version = runtime.sdk.get_document_version(
            binding.project_id,
            binding.knowledge_base_id,
            reference.document_id,
            reference.document_version_id,
        )
        if version.source_artifact_id is None:
            raise NotFound(
                "该版本原件不可用。", stage="wanshitong.public.reference"
            )
        blob = runtime.sdk.read_artifact(
            binding.project_id,
            binding.knowledge_base_id,
            reference.document_id,
            reference.document_version_id,
            version.source_artifact_id,
        )
        return Response(
            content=blob.content,
            media_type=blob.media_type,
            headers={"Cache-Control": "no-store"},
        )

    def _resolve_natural_reference(
        request: Request,
        conversation_id: str,
        trace_id: str,
        reference_id: str,
    ) -> tuple[NaturalReference, dict[str, object]]:
        if not natural_public_enabled:
            raise HTTPException(
                status_code=404, detail="natural reference disabled"
            )
        principal, _ = _authenticate_public_request(request, sessions)
        binding = scope_service.binding()
        scope = KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        record = runtime.conversations.natural_turn(
            scope, conversation_id, trace_id, owner_id=principal.owner_id
        )
        if record is None or record.status != "ANSWERED":
            raise NotFound("来源不可用。", stage="wanshitong.public.reference")
        reference = next(
            (
                item
                for item in record.references
                if natural_reference_id(trace_id, item.alias) == reference_id
            ),
            None,
        )
        if reference is None:
            raise NotFound("来源不可用。", stage="wanshitong.public.reference")
        citation = public_natural_references(trace_id, (reference,))[0]
        return reference, citation

    @app.delete(
        PUBLIC_CONVERSATION_PATH,
        tags=["wanshitong-public"],
        response_model=PublicConversationClearResponse,
    )
    def _clear_conversation(
        conversation_id: Annotated[
            str,
            Path(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            ),
        ],
        request: Request,
    ) -> PublicConversationClearResponse:
        _reject_query_parameters(request)
        principal, _ = _authenticate_public_request(request, sessions)
        binding = scope_service.binding()
        scope = KnowledgeBaseScope(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        with runtime.conversations.lease(
            scope, conversation_id, owner_id=principal.owner_id
        ):
            result = runtime.conversations.clear(
                scope, conversation_id, owner_id=principal.owner_id
            )
        if not result.deleted:
            raise NotFound(
                "公共会话不存在。", stage="wanshitong.public.conversation"
            )
        return PublicConversationClearResponse(
            conversation_id=conversation_id,
            deleted_turns=result.deleted_turns,
        )

    @app.post(
        PUBLIC_FEEDBACK_PATH,
        tags=["wanshitong-public"],
        response_model=PublicFeedbackResponse,
    )
    def _feedback(
        body: PublicFeedbackRequest,
        request: Request,
    ) -> PublicFeedbackResponse:
        _reject_query_parameters(request)
        principal, _ = _authenticate_public_request(request, sessions)
        binding = scope_service.binding()
        value = runtime.wanshitong_feedback.upsert(
            body.trace_id,
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            actor_owner_id=principal.owner_id,
            useful=body.useful,
            reason_code=body.reason_code,
            reason_detail=body.reason_detail,
            comment=body.comment,
        )
        return PublicFeedbackResponse(
            trace_id=value.trace_id,
            useful=value.useful,
            reason_code=value.reason_code,
            reason_detail=value.reason_detail,
            comment_saved=value.comment_saved,
            feedback_revision=value.feedback_revision,
            projection_state=value.projection_state,
            updated_at=value.updated_at,
        )


def _authenticate_public_request(
    request: Request, sessions: PublicSessionProvider
) -> tuple[PublicSessionPrincipal, str]:
    cookie_value = request.cookies.get(sessions.cookie_name)
    if cookie_value is None:
        raise HTTPException(status_code=401, detail="public session required")
    try:
        sessions.validate_cookie(cookie_value)
    except PolicyDenied:
        raise HTTPException(
            status_code=401, detail="public session invalid"
        ) from None
    csrf_token = request.headers.get("X-CSRF-Token")
    if csrf_token is None:
        raise HTTPException(status_code=403, detail="public csrf required")
    try:
        principal = sessions.authenticate(cookie_value, csrf_token)
    except PolicyDenied:
        raise HTTPException(
            status_code=403, detail="public csrf invalid"
        ) from None
    return principal, cookie_value


def _authenticate_public_cookie(
    request: Request, sessions: PublicSessionProvider
) -> PublicSessionPrincipal:
    cookie_value = request.cookies.get(sessions.cookie_name)
    if cookie_value is None:
        raise HTTPException(status_code=401, detail="public session required")
    try:
        return sessions.validate_cookie(cookie_value)
    except PolicyDenied:
        raise HTTPException(
            status_code=401, detail="public session invalid"
        ) from None


def _reject_query_parameters(request: Request) -> None:
    if request.query_params:
        raise ValueError("公共 API 不接受 Query 参数。")


def _validate_stream_session(
    sessions: PublicSessionProvider,
    cookie_value: str,
    expected: PublicSessionPrincipal,
) -> None:
    current = sessions.validate_cookie(cookie_value)
    if current != expected:
        raise PolicyDenied(
            "公共会话已变化。", stage="wanshitong.public.session"
        )


__all__ = [
    "PUBLIC_CAPABILITIES_PATH",
    "PUBLIC_CHAT_PATH",
    "PUBLIC_CONVERSATIONS_PATH",
    "PUBLIC_CONVERSATION_PATH",
    "PUBLIC_FEEDBACK_PATH",
    "PUBLIC_POPULAR_QUESTIONS_PATH",
    "PUBLIC_SESSION_PATH",
    "register_public_routes",
]
