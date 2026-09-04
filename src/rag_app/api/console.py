"""P10 控制台的管理员只读 API 路由。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, Header, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from rag_app.composition.p09_runtime import P09Runtime
from rag_app.core.models import (
    ChunkPage,
    JobPage,
    JobStatus,
    RevisionInspection,
)


class RevisionReportsResponse(BaseModel):
    """Revision 文档质量报告集合。"""

    items: list[dict[str, object]]


def register_console_routes(
    app: FastAPI,
    runtime: P09Runtime,
    require_admin: Callable[[str | None], None],
) -> None:
    """注册只读 Job、Revision、Chunk 与报告路由。

    Args:
        app: P09 FastAPI 应用。
        runtime: 共享 P09 Application Services 的运行时。
        require_admin: P09 管理员鉴权函数。

    Returns:
        无返回值。

    """

    @app.get("/api/v1/jobs", tags=["jobs"], response_model=JobPage)
    def _list_jobs(  # noqa: PLR0913, PLR0917
        authorization: Annotated[str | None, Header()] = None,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        state: Annotated[list[JobStatus] | None, Query()] = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        """按 scope 和公开状态分页读取 Job。"""
        require_admin(authorization)
        page = runtime.sdk.list_jobs(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            states=tuple(item.value for item in state or ()),
            page_size=page_size,
            offset=offset,
        )
        return dict(jsonable_encoder(page.model_dump(mode="json")))

    base = (
        "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}/"
        "revisions/{revision_id}"
    )

    @app.get(base, tags=["revisions"], response_model=RevisionInspection)
    def _inspect_revision(
        project_id: str,
        kb_id: str,
        revision_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取 scope 绑定的 Revision 状态和实际计数。"""
        require_admin(authorization)
        result = runtime.sdk.inspect_revision(project_id, kb_id, revision_id)
        return dict(jsonable_encoder(result.model_dump(mode="json")))

    @app.get(base + "/chunks", tags=["revisions"], response_model=ChunkPage)
    def _list_chunks(  # noqa: PLR0913, PLR0917
        project_id: str,
        kb_id: str,
        revision_id: str,
        authorization: Annotated[str | None, Header()] = None,
        document_id: str | None = None,
        role: str | None = None,
        section_id: str | None = None,
        neighbor_group_id: str | None = None,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        """读取 canonical Chunk 三视图和精确 SourceSpan。"""
        require_admin(authorization)
        page = runtime.sdk.list_revision_chunks(
            project_id,
            kb_id,
            revision_id,
            document_id=document_id,
            role=role,
            section_id=section_id,
            neighbor_group_id=neighbor_group_id,
            page_size=page_size,
            offset=offset,
        )
        return dict(jsonable_encoder(page.model_dump(mode="json")))

    @app.get(
        base + "/reports",
        tags=["revisions"],
        response_model=RevisionReportsResponse,
    )
    def _reports(
        project_id: str,
        kb_id: str,
        revision_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """读取每个文档的 ParseReport 与 ChunkingReport。"""
        require_admin(authorization)
        items = runtime.sdk.revision_document_reports(
            project_id, kb_id, revision_id
        )
        return {
            "items": [
                dict(jsonable_encoder(item.model_dump(mode="json")))
                for item in items
            ]
        }


__all__ = ["register_console_routes"]
