"""已有管理员会话控制的追加预算授权入口。"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request
from pydantic import TypeAdapter

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_revision import (
    BudgetAuthorizationRevision,
)
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.product.auth import SESSION_COOKIE
from rag_app.product.budget_approval import approve_budget_revision

_REVISION = TypeAdapter(BudgetAuthorizationRevision)
_REVISION_SCHEMA = _REVISION.json_schema()
_REVISION_SCHEMA["additionalProperties"] = False
_REVISION_SCHEMA["properties"]["status"]["enum"] = ["APPROVED"]


def register_provider_budget_routes(
    app: FastAPI, runtime: ProductRuntime
) -> None:
    """注册现有产品认证和审计边界内的预算修订动作。

    Args:
        app: 已安装产品管理员认证中间件的应用。
        runtime: 当前产品实例；不创建新实例或解析 Provider 凭据。

    Returns:
        无返回值。

    """

    @app.get("/api/v1/provider-budget/campaign", tags=["provider-budget"])
    def _campaign(request: Request) -> dict[str, object]:
        if getattr(request.state, "product_principal", None) != "admin_session":
            raise HTTPException(403, "BUDGET_ADMIN_SESSION_REQUIRED")
        path = runtime.settings.data_dir / "provider-budget.sqlite3"
        if not path.is_file():
            raise HTTPException(409, "CAMPAIGN_BINDING_REQUIRED")
        try:
            reader = ProviderBudgetLedger(path, read_only=True)
            campaign_id = reader.active_campaign_id()
            if campaign_id is None:
                raise BudgetBlockedError("CAMPAIGN_BINDING_REQUIRED")
            return reader.authorization_snapshot(campaign_id)
        except BudgetBlockedError as error:
            raise HTTPException(409, error.reason) from None

    @app.post(
        "/api/v1/provider-budget/revisions",
        tags=["provider-budget"],
        summary="由已登录管理员明确批准累计预算修订",
        description=(
            "必须使用现有管理员 Session Cookie 和 X-CSRF-Token；"
            "受限 API Token 与计划文件不能扩额。approver 必须由实际"
            "授权人明确填写；全部数值是保留历史消费的累计总上限。"
        ),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _REVISION_SCHEMA}},
            },
        },
    )
    async def _approve(request: Request) -> dict[str, object]:
        if getattr(request.state, "product_principal", None) != "admin_session":
            raise HTTPException(403, "BUDGET_ADMIN_SESSION_REQUIRED")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - set(
                BudgetAuthorizationRevision.__dataclass_fields__
            ):
                raise ValueError("审批字段无效。")
            revision = _REVISION.validate_json(json.dumps(body), strict=True)
        except ValueError:
            raise HTTPException(400, "BUDGET_REVISION_BODY_INVALID") from None
        path = runtime.settings.data_dir / "provider-budget.sqlite3"
        if not path.is_file():
            raise HTTPException(409, "CAMPAIGN_BINDING_REQUIRED")
        try:
            reader = ProviderBudgetLedger(path, read_only=True)
            if reader.active_campaign_id() != revision.campaign_id:
                raise BudgetBlockedError("BUDGET_REVISION_SCOPE_MISMATCH")
            ledger = ProviderBudgetLedger(path)
            approve_budget_revision(
                ledger,
                revision,
                auth=runtime.auth,
                session_token=request.cookies.get(SESSION_COOKIE, ""),
                csrf_token=request.headers.get("X-CSRF-Token", ""),
            )
            return {
                "revision_id": revision.revision_id,
                "status": "APPROVED",
                "budget": ledger.summary(revision.campaign_id),
            }
        except BudgetBlockedError as error:
            raise HTTPException(409, error.reason) from None
        except ValueError:
            raise HTTPException(400, "BUDGET_REVISION_BODY_INVALID") from None
