"""Product 多轮会话清理与 Query Feedback API。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import PolicyDenied
from rag_app.core.models import KnowledgeBaseScope
from rag_app.product.feedback import ProductFeedback


class FeedbackRequest(BaseModel):
    """只允许布尔信号与有限原因码。"""

    model_config = ConfigDict(extra="forbid")

    useful: bool
    reason_code: (
        Literal[
            "INCORRECT",
            "INCOMPLETE",
            "WRONG_SOURCE",
            "OUTDATED",
            "TOO_SLOW",
            "OTHER",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _validate_reason(self) -> FeedbackRequest:
        if self.useful and self.reason_code is not None:
            raise ValueError("有用反馈不应附带负向 reason_code。")
        return self


class FeedbackResponse(BaseModel):
    """canonical 反馈与 Trace 投影对账状态。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    useful: bool
    reason_code: str | None
    projection_state: Literal["PENDING", "APPLIED", "NOT_APPLICABLE"]
    updated_at: str


class FeedbackReadResponse(BaseModel):
    """尚未提交时仍返回稳定空状态。"""

    model_config = ConfigDict(extra="forbid")

    feedback: FeedbackResponse | None


class ConversationClearResponse(BaseModel):
    """严格 scope 会话清理回执。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    owner_id: str
    deleted: bool
    deleted_turns: int = Field(ge=0)


def register_conversation_feedback_routes(
    app: FastAPI, runtime: ProductRuntime
) -> None:
    """注册只复用 Product 鉴权中间件的 scoped API。

    Args:
        app: 已启用 Product Session、CSRF 与 Token Scope 的应用。
        runtime: 当前 Product 组合根。

    Returns:
        路由注册完成时无返回值。

    """
    base = "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"

    @app.delete(
        base + "/conversations/{conversation_id}",
        tags=["conversation"],
        response_model=ConversationClearResponse,
    )
    def _clear_conversation(
        project_id: str,
        kb_id: str,
        conversation_id: str,
        request: Request,
        owner_id: Annotated[str | None, Query(max_length=256)] = None,
    ) -> ConversationClearResponse:
        runtime.sdk.get_knowledge_base(project_id, kb_id)
        actor = _owner(request)
        if (
            owner_id is not None
            and not _is_admin(request)
            and owner_id != actor
        ):
            raise PolicyDenied(
                "只能清理当前主体自己的会话。", stage="conversation.owner"
            )
        resolved_owner = owner_id if _is_admin(request) and owner_id else actor
        scope = KnowledgeBaseScope(
            project_id=project_id,
            knowledge_base_id=kb_id,
        )
        with runtime.conversations.lease(
            scope,
            conversation_id,
            owner_id=resolved_owner,
        ):
            result = runtime.conversations.clear(
                scope,
                conversation_id,
                owner_id=resolved_owner,
            )
        return ConversationClearResponse(
            conversation_id=conversation_id,
            owner_id=resolved_owner,
            deleted=result.deleted,
            deleted_turns=result.deleted_turns,
        )

    @app.get(
        base + "/queries/{trace_id}/feedback",
        tags=["feedback"],
        response_model=FeedbackReadResponse,
    )
    def _get_feedback(
        project_id: str,
        kb_id: str,
        trace_id: str,
        request: Request,
    ) -> FeedbackReadResponse:
        value = runtime.feedback.get(
            trace_id,
            project_id=project_id,
            knowledge_base_id=kb_id,
            actor_owner_id=_owner(request),
            actor_is_admin=_is_admin(request),
        )
        return FeedbackReadResponse(
            feedback=None if value is None else _feedback_response(value)
        )

    @app.put(
        base + "/queries/{trace_id}/feedback",
        tags=["feedback"],
        response_model=FeedbackResponse,
    )
    def _put_feedback(
        project_id: str,
        kb_id: str,
        trace_id: str,
        body: FeedbackRequest,
        request: Request,
    ) -> FeedbackResponse:
        value = runtime.feedback.upsert(
            trace_id,
            project_id=project_id,
            knowledge_base_id=kb_id,
            actor_owner_id=_owner(request),
            actor_is_admin=_is_admin(request),
            useful=body.useful,
            reason_code=body.reason_code,
        )
        return _feedback_response(value)


def _owner(request: Request) -> str:
    return str(getattr(request.state, "access_token_id", "local-admin"))


def _is_admin(request: Request) -> bool:
    return getattr(request.state, "product_principal", None) in {
        "admin_session",
        "legacy_admin",
    }


def _feedback_response(value: ProductFeedback) -> FeedbackResponse:
    return FeedbackResponse(
        trace_id=value.trace_id,
        project_id=value.project_id,
        knowledge_base_id=value.knowledge_base_id,
        useful=value.useful,
        reason_code=value.reason_code,
        projection_state=value.projection_state,
        updated_at=value.updated_at,
    )


__all__ = [
    "ConversationClearResponse",
    "FeedbackReadResponse",
    "FeedbackRequest",
    "FeedbackResponse",
    "register_conversation_feedback_routes",
]
