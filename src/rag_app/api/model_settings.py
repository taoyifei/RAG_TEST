"""本机管理员选择知识库模型的最小接口。"""

from fastapi import FastAPI, HTTPException, Response
from pydantic import Field

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.models.common import FrozenModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings


class OcrConfirmation(FrozenModel):
    """用户从当前盘点结果中选出的媒体，不扩大既有出网授权。"""

    confirmed_media_hashes: tuple[str, ...] = Field(
        min_length=1, max_length=200
    )


def register_model_settings_routes(
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
        return {
            **settings.model_dump(),
            "generation_configured": bool(settings.generation_connection_id),
            "ocr_configured": bool(
                settings.ocr_connection_id and settings.ocr_enabled
            ),
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

    @app.get(ocr_path, tags=["ocr"])
    def _scan(knowledge_base_id: str, document_id: str) -> dict[str, object]:
        return runtime.ocr.scan(knowledge_base_id, document_id)

    @app.post(ocr_path, tags=["ocr"], status_code=202)
    def _recognize(
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
        pending = {
            str(item["media_sha256"])
            for item in media
            if isinstance(item, dict) and not item.get("cached")
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
            media_type=source.media_type,
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
