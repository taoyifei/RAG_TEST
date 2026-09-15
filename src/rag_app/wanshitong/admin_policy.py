"""湾事通模式下锁定 Universal 模型配置写接口。"""

from __future__ import annotations

import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

MODEL_CONFIGURATION_LOCKED = "MODEL_CONFIGURATION_LOCKED"
_LOCKED_MESSAGE = "湾事通模式下模型配置由服务端初始化流程管理。"
_LOCKED_ROUTES = (
    ("POST", re.compile(r"^/api/v1/provider-credentials$")),
    ("POST", re.compile(r"^/api/v1/provider-credentials/[^/]+:rotate$")),
    ("POST", re.compile(r"^/api/v1/provider-connections$")),
    ("PATCH", re.compile(r"^/api/v1/provider-connections/[^/]+$")),
    ("POST", re.compile(r"^/api/v1/provider-connections/[^/]+:validate$")),
    (
        "POST",
        re.compile(r"^/api/v1/knowledge-bases/[^/]+/retrieval-profiles$"),
    ),
    (
        "POST",
        re.compile(
            r"^/api/v1/retrieval-profiles/[^/]+/authorization:approve$"
        ),
    ),
    (
        "POST",
        re.compile(r"^/api/v1/jobs/[^/]+/retrieval-authorization:approve$"),
    ),
    ("POST", re.compile(r"^/api/v1/retrieval-profiles/[^/]+:activate$")),
    ("PUT", re.compile(r"^/api/v1/knowledge-bases/[^/]+/model-settings$")),
    (
        "POST",
        re.compile(
            r"^/api/v1/knowledge-bases/[^/]+/corpus-authorization:approve$"
        ),
    ),
    ("POST", re.compile(r"^/api/v1/provider-budget/revisions$")),
    (
        "POST",
        re.compile(r"^/api/v1/knowledge-bases/[^/]+/documents/[^/]+/ocr$"),
    ),
)


def model_configuration_lock_response(request: Request) -> Response | None:
    """为已认证湾事通请求返回稳定模型配置锁，其他请求不处理。"""
    if not getattr(request.app.state, "wanshitong_enabled", False):
        return None
    if not any(
        request.method == method and pattern.fullmatch(request.url.path)
        for method, pattern in _LOCKED_ROUTES
    ):
        return None
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": MODEL_CONFIGURATION_LOCKED,
                "message": _LOCKED_MESSAGE,
                "stage": "wanshitong.model.configuration",
                "retryable": False,
                "trace_id": "",
                "details": {},
            }
        },
    )


__all__ = [
    "MODEL_CONFIGURATION_LOCKED",
    "model_configuration_lock_response",
]
