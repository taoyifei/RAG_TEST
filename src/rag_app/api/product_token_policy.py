"""外部 Token 的显式路由能力表；未列出的控制面仅供管理员。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from starlette.routing import compile_path

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import NotFound, PolicyDenied

_BASE = "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"
_READ_PATHS = (
    "/api/v1/projects/{project_id}",
    "/api/v1/projects/{project_id}/knowledge-bases",
    _BASE,
    _BASE + "/documents",
    _BASE + "/documents/{document_id}",
    _BASE + "/documents/{document_id}/versions",
    _BASE + "/documents/{document_id}/versions/{dver}",
    _BASE + "/documents/{document_id}/versions/{dver}/artifacts",
    _BASE + "/artifacts/{artifact_id}",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs",
    _BASE + "/revisions/{revision_id}",
    _BASE + "/revisions/{revision_id}/chunks",
    _BASE + "/revisions/{revision_id}/reports",
)
_WRITE_ROUTES = (
    ("POST", _BASE + "/documents"),
    ("PATCH", _BASE + "/documents/{document_id}"),
    ("DELETE", _BASE + "/documents/{document_id}"),
    ("POST", _BASE + "/documents/{document_id}/versions"),
    ("POST", "/api/v1/jobs/{job_id}:cancel"),
    ("POST", "/api/v1/jobs/{job_id}:retry"),
)


@dataclass(frozen=True)
class TokenRoute:
    """完全匹配方法和模板后的授权输入。"""

    scope: str
    project_id: str | None
    knowledge_base_id: str | None


def resolve_token_route(
    request: Request, runtime: ProductRuntime
) -> TokenRoute:
    """先解析资源归属，再提供 Scope；不读取 Artifact 内容。

    Args:
        request: 尚未注入内部凭据的外部请求。
        runtime: 当前隔离或生产控制面。

    Returns:
        路由要求的 Scope 和可靠资源归属。

    Raises:
        PolicyDenied: 未列入外部能力表或资源无法安全解析。

    """
    routes = (
        *(("GET", path, "knowledge:read") for path in _READ_PATHS),
        *((method, path, "knowledge:write") for method, path in _WRITE_ROUTES),
        ("POST", _BASE + ":search", "query:read"),
        ("POST", _BASE + ":answer", "query:read"),
        ("GET", "/api/v1/system/components", "system:read"),
    )
    for method, template, scope in routes:
        match = compile_path(template)[0].fullmatch(request.url.path)
        if request.method != method or match is None:
            continue
        params = match.groupdict()
        project_id = params.get("project_id")
        kb_id = params.get("kb_id")
        try:
            if template == "/api/v1/jobs":
                project_id = request.query_params.get("project_id")
                kb_id = request.query_params.get("knowledge_base_id")
                if not project_id or not kb_id:
                    raise PolicyDenied(
                        "任务列表必须绑定完整范围。", stage="token.scope"
                    )
            if "job_id" in params:
                job = runtime.p09.store.get_job(params["job_id"])
                project_id, kb_id = job.project_id, job.knowledge_base_id
            if project_id is not None and kb_id is not None:
                runtime.p09.store.get_knowledge_base(project_id, kb_id)
            if "artifact_id" in params:
                if project_id is None or kb_id is None:
                    raise PolicyDenied("资源范围缺失。", stage="token.scope")
                runtime.p09.store.authorize_artifact(
                    project_id,
                    kb_id,
                    request.query_params.get("document_id", ""),
                    request.query_params.get("document_version_id", ""),
                    params["artifact_id"],
                )
        except NotFound:
            raise PolicyDenied(
                "资源范围不匹配。", stage="token.scope"
            ) from None
        return TokenRoute(scope, project_id, kb_id)
    raise PolicyDenied("此接口仅允许管理员。", stage="token.route")
