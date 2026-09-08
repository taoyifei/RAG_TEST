"""本机管理员选择知识库模型的最小接口。"""

from threading import RLock
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import Field

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.models.common import FrozenModel
from rag_app.product.diagram_relations import (
    DiagramRelationCandidate,
    RelationReviewState,
)
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.product.ocr_adapters import LOCAL_OCR_CONNECTION_ID


class OcrConfirmation(FrozenModel):
    """用户从当前盘点结果中选出的媒体，不扩大既有出网授权。"""

    confirmed_media_hashes: tuple[str, ...] = Field(
        min_length=1, max_length=200
    )


class DiagramRelationReview(FrozenModel):
    """管理员对结构化图关系候选作出的显式发布决定。"""

    state: Literal["accepted", "rejected", "ambiguous"]


class DiagramRelationReviewResponse(FrozenModel):
    """审阅后的候选与可能触发的新 Revision Job。"""

    candidate: DiagramRelationCandidate
    rebuild_job_id: str | None = None
    content_identity: str | None = None


def register_model_settings_routes(  # noqa: PLR0915
    app: FastAPI, runtime: ProductRuntime
) -> None:
    """沿用产品会话与 CSRF 权限，不通过保存配置触发计费。

    Args:
        app: 已启用会话和 CSRF 校验的应用。
        runtime: 当前唯一产品运行时。

    Returns:
        注册完成时无返回值。

    """
    path = "/api/v1/knowledge-bases/{knowledge_base_id}/model-settings"

    @app.get(path, tags=["models"])
    def _get(knowledge_base_id: str) -> dict[str, object]:
        settings = runtime.models.get(knowledge_base_id)
        local_ocr_selected = (
            settings.ocr_connection_id == LOCAL_OCR_CONNECTION_ID
        )
        return {
            **settings.model_dump(),
            "generation_configured": bool(settings.generation_connection_id),
            "ocr_configured": bool(
                settings.ocr_connection_id and settings.ocr_enabled
            )
            and (
                not local_ocr_selected or runtime.providers.local_ocr_available
            ),
            "local_ocr_available": runtime.providers.local_ocr_available,
        }

    @app.put(path, tags=["models"])
    def _save(
        knowledge_base_id: str, settings: KnowledgeBaseModelSettings
    ) -> dict[str, object]:
        if "ocr_media_hashes" not in settings.model_fields_set:
            settings = settings.model_copy(
                update={
                    "ocr_media_hashes": runtime.models.get(
                        knowledge_base_id
                    ).ocr_media_hashes,
                }
            )
        runtime.models.save(knowledge_base_id, settings)
        return _get(knowledge_base_id)

    ocr_path = (
        "/api/v1/knowledge-bases/{knowledge_base_id}"
        "/documents/{document_id}/ocr"
    )
    ocr_submission_lock = RLock()

    @app.get(ocr_path, tags=["ocr"])
    def _scan(knowledge_base_id: str, document_id: str) -> dict[str, object]:
        return runtime.ocr.scan(knowledge_base_id, document_id)

    @app.post(ocr_path, tags=["ocr"], status_code=202)
    def _recognize(
        knowledge_base_id: str, document_id: str, confirmation: OcrConfirmation
    ) -> dict[str, object]:
        # 仅串行化设置冻结和入队；实际 OCR 仍由持久队列执行。
        with ocr_submission_lock:
            return _recognize_locked(
                knowledge_base_id, document_id, confirmation
            )

    def _recognize_locked(
        knowledge_base_id: str, document_id: str, confirmation: OcrConfirmation
    ) -> dict[str, object]:
        settings = runtime.models.get(knowledge_base_id)
        if not settings.ocr_enabled or not settings.ocr_connection_id:
            raise HTTPException(409, "请先配置并启用图片识别。")
        scan = runtime.ocr.scan(knowledge_base_id, document_id)
        media = scan["media"]
        if not isinstance(media, (list, tuple)):
            raise HTTPException(409, "媒体盘点不可用。")
        permitted = {
            str(item["media_sha256"])
            for item in media
            if isinstance(item, dict)
            and item.get("supported")
            and item.get("approved")
        }
        selected = set(confirmation.confirmed_media_hashes)
        if not selected <= permitted:
            raise HTTPException(403, "所选图片未获批准或不受支持。")
        if selected <= set(settings.ocr_media_hashes):
            inflight = _inflight_ocr_job(
                runtime, knowledge_base_id, document_id
            )
            if inflight is not None:
                return runtime.sdk.get_job(inflight).model_dump(mode="json")
        pending = {
            str(item["media_sha256"])
            for item in media
            if isinstance(item, dict) and not item.get("indexed")
        }
        settings = settings.model_copy(
            update={
                "ocr_media_hashes": tuple(
                    sorted(set(settings.ocr_media_hashes) | selected)
                ),
                "ocr_revision": settings.ocr_revision
                + bool(selected & pending),
            }
        )
        runtime.models.save(knowledge_base_id, settings)
        with runtime.connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM knowledge_bases "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "知识库不存在。")
        project_id = str(row[0])
        document = runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        if document.current_version_id is None:
            raise HTTPException(409, "文档尚无可检索版本，请先完成原生入库。")
        version = runtime.sdk.get_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            document.current_version_id,
        )
        source = runtime.sdk.read_artifact(
            project_id,
            knowledge_base_id,
            document_id,
            version.document_version_id,
            version.source_artifact_id,
        )
        job = runtime.sdk.create_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            content=source.content,
            # 版本保留上传 MIME；共享 Blob 的解析制品 MIME 可以不同。
            media_type=version.media_type,
            idempotency_key="ocr-"
            + str(runtime.ocr.content_identity(knowledge_base_id)),
        )
        return job.model_dump(mode="json")

    image_path = (
        "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"
        "/documents/{document_id}/images/{artifact_id}"
    )

    @app.get(image_path, tags=["ocr"])
    def _image(
        project_id: str, kb_id: str, document_id: str, artifact_id: str
    ) -> Response:
        document = runtime.sdk.get_document(project_id, kb_id, document_id)
        if document.current_version_id is None:
            raise HTTPException(404, "文档没有可用图片版本。")
        image = runtime.sdk.read_artifact(
            project_id,
            kb_id,
            document_id,
            document.current_version_id,
            artifact_id,
        )
        if image.media_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        }:
            raise HTTPException(415, "当前图片格式不支持浏览器预览。")
        return Response(
            content=image.content,
            media_type=image.media_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    relation_path = (
        "/api/v1/knowledge-bases/{knowledge_base_id}"
        "/documents/{document_id}/diagram-relations"
    )

    @app.get(
        relation_path,
        tags=["diagram-relations"],
        response_model=list[DiagramRelationCandidate],
    )
    def _relations(
        knowledge_base_id: str, document_id: str
    ) -> list[DiagramRelationCandidate]:
        project_id = _document_project(runtime, knowledge_base_id, document_id)
        runtime.sdk.get_document(project_id, knowledge_base_id, document_id)
        return list(
            runtime.relations.list_candidates(knowledge_base_id, document_id)
        )

    @app.post(
        relation_path + "/{candidate_id}:review",
        tags=["diagram-relations"],
        response_model=DiagramRelationReviewResponse,
        status_code=202,
    )
    def _review_relation(
        knowledge_base_id: str,
        document_id: str,
        candidate_id: str,
        body: DiagramRelationReview,
        request: Request,
    ) -> DiagramRelationReviewResponse:
        candidates = runtime.relations.list_candidates(
            knowledge_base_id, document_id
        )
        if candidate_id not in {item.candidate_id for item in candidates}:
            raise HTTPException(404, "图关系候选不存在。")
        previous_identity = runtime.relations.content_identity(
            knowledge_base_id
        )
        reviewer_id = str(
            getattr(request.state, "access_token_id", None)
            or getattr(request.state, "product_principal", "admin_session")
        )
        reviewed = runtime.relations.review(
            candidate_id,
            RelationReviewState(body.state),
            reviewer_id=reviewer_id,
        )
        current_identity = runtime.relations.content_identity(knowledge_base_id)
        job_id = None
        if current_identity != previous_identity:
            job_id = _queue_relation_rebuild(
                runtime,
                knowledge_base_id,
                document_id,
                content_identity=current_identity,
            )
        return DiagramRelationReviewResponse(
            candidate=reviewed,
            rebuild_job_id=job_id,
            content_identity=current_identity,
        )


def _inflight_ocr_job(
    runtime: ProductRuntime, knowledge_base_id: str, document_id: str
) -> str | None:
    """同一冻结内容配置的重复请求复用未结束 Job，不改变其 fencing。"""
    identity = runtime.content_identity(knowledge_base_id)
    with runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT j.job_id FROM ingestion_jobs j "
            "JOIN ingestion_requests r ON r.job_id=j.job_id "
            "WHERE j.knowledge_base_id=? AND j.document_id=? "
            "AND j.cancel_requested=0 "
            "AND r.state IN ('queued', 'running') "
            "AND json_extract(r.request_json, '$.content_identity')=? "
            "ORDER BY j.created_at DESC LIMIT 1",
            (knowledge_base_id, document_id, identity),
        ).fetchone()
    return None if row is None else str(row[0])


def _queue_relation_rebuild(
    runtime: ProductRuntime,
    knowledge_base_id: str,
    document_id: str,
    *,
    content_identity: str | None,
) -> str:
    """用原文档字节创建新索引 Revision，不原地污染 active index。"""
    project_id = _document_project(runtime, knowledge_base_id, document_id)
    document = runtime.sdk.get_document(
        project_id, knowledge_base_id, document_id
    )
    if document.current_version_id is None:
        raise HTTPException(409, "文档尚无可检索版本。")
    version = runtime.sdk.get_document_version(
        document.project_id,
        knowledge_base_id,
        document_id,
        document.current_version_id,
    )
    source = runtime.sdk.read_artifact(
        document.project_id,
        knowledge_base_id,
        document_id,
        version.document_version_id,
        version.source_artifact_id,
    )
    job = runtime.sdk.create_document_version(
        document.project_id,
        knowledge_base_id,
        document_id,
        content=source.content,
        media_type=version.media_type,
        idempotency_key="diagram-relations-" + str(content_identity),
    )
    return job.job_id


def _document_project(
    runtime: ProductRuntime, knowledge_base_id: str, document_id: str
) -> str:
    """从 Product 主库解析文档范围，避免信任客户端项目身份。"""
    with runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT project_id FROM documents WHERE knowledge_base_id=? "
            "AND document_id=? AND deleted_at IS NULL AND status='active'",
            (knowledge_base_id, document_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "文档不存在。")
    return str(row[0])
