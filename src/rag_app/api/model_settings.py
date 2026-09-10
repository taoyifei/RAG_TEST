"""本机管理员选择知识库模型的最小接口。"""

from threading import RLock
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import Field

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import RagError
from rag_app.core.models import EmbeddingTopology, RetrievalPolicy
from rag_app.core.models.common import FrozenModel
from rag_app.product.corpus_authorization import (
    CorpusAuthorizationApproval,
    CorpusAuthorizationStatus,
)
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
    authorization_path = (
        "/api/v1/knowledge-bases/{knowledge_base_id}/corpus-authorization"
    )

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
            "corpus_authorization": runtime.corpus_authorizations.status(
                knowledge_base_id
            ).model_dump(mode="json"),
            "retrieval_data_plane": _retrieval_data_plane_status(
                runtime, knowledge_base_id
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
        runtime.profiles.invalidate(knowledge_base_id)
        return _get(knowledge_base_id)

    @app.get(
        authorization_path,
        tags=["models"],
        response_model=CorpusAuthorizationStatus,
    )
    def _authorization_status(
        knowledge_base_id: str,
    ) -> CorpusAuthorizationStatus:
        """读取当前活动语料、模型用途和累计预算的动态对账状态。"""
        return runtime.corpus_authorizations.status(knowledge_base_id)

    @app.post(
        authorization_path + ":approve",
        tags=["models"],
        response_model=CorpusAuthorizationStatus,
    )
    def _approve_authorization(
        knowledge_base_id: str,
        approval: CorpusAuthorizationApproval,
        request: Request,
    ) -> CorpusAuthorizationStatus:
        """仅接受管理员会话对服务端冻结的活动语料作明确批准。"""
        if getattr(request.state, "product_principal", None) != "admin_session":
            raise HTTPException(403, "资料授权只能由控制台管理员会话批准。")
        session_id = getattr(request.state, "product_session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(403, "管理员会话身份不可用。")
        try:
            status = runtime.corpus_authorizations.approve(
                knowledge_base_id,
                approval,
                approved_by_session_id=session_id,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        runtime.profiles.invalidate(knowledge_base_id)
        return status

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


def _retrieval_data_plane_status(
    runtime: ProductRuntime, knowledge_base_id: str
) -> dict[str, object]:
    """在不出网的前提下对账当前 Profile、Revision 和向量覆盖。

    Args:
        runtime: 当前唯一产品运行时。
        knowledge_base_id: 用户正在查看的知识库。

    Returns:
        可在首次问答前展示的非敏感实际检索数据面状态。

    """
    profiles = runtime.control.list_profiles(knowledge_base_id)
    active = next((item for item in profiles if item.status == "active"), None)
    with runtime.connections.transaction() as connection:
        revision = connection.execute(
            "SELECT kb.active_revision_id,r.index_fingerprint,"
            "r.embedding_topology_json,r.expected_chunk_count "
            "FROM knowledge_bases kb LEFT JOIN index_revisions r "
            "ON r.index_revision_id=kb.active_revision_id "
            "WHERE kb.knowledge_base_id=? AND kb.deleted_at IS NULL",
            (knowledge_base_id,),
        ).fetchone()
        if revision is None:
            raise HTTPException(404, "知识库不存在。")
        coverage_rows = (
            ()
            if revision["active_revision_id"] is None
            else connection.execute(
                "SELECT slot_id,expected_chunk_count,valid_vector_count,state "
                "FROM revision_embedding_coverage WHERE revision_id=? "
                "ORDER BY slot_id",
                (revision["active_revision_id"],),
            ).fetchall()
        )
        latest_draft = next(
            (item for item in profiles if item.status == "draft"), None
        )
        activation_state = None
        if latest_draft is not None and latest_draft.activation_job_id:
            job = connection.execute(
                "SELECT state FROM ingestion_jobs WHERE job_id=?",
                (latest_draft.activation_job_id,),
            ).fetchone()
            activation_state = None if job is None else str(job[0])
    topology = (
        None
        if revision["embedding_topology_json"] is None
        else EmbeddingTopology.model_validate_json(
            str(revision["embedding_topology_json"])
        )
    )
    primary = None if topology is None else topology.slot(topology.primary_slot_id)
    reasons: list[str] = []
    if active is None:
        reasons.append("NO_ACTIVE_RETRIEVAL_PROFILE")
        if primary is not None and primary.provider_id.casefold().startswith(
            "deterministic"
        ):
            reasons.append("DETERMINISTIC_EMBEDDING")
    expected_chunks = int(revision["expected_chunk_count"] or 0)
    coverage_complete = bool(coverage_rows) and all(
        int(row["expected_chunk_count"]) == expected_chunks
        and int(row["valid_vector_count"]) == expected_chunks
        and str(row["state"]) == "complete"
        for row in coverage_rows
    )
    if revision["active_revision_id"] is not None and not coverage_complete:
        reasons.append("VECTOR_COVERAGE_INCOMPLETE")
    calibration_state = "UNCALIBRATED"
    reranker_provider_id = "lexical_overlap"
    reranker_model = "1"
    serving_fingerprint = runtime.sdk.health().serving_fingerprint
    profile_state = "NOT_CONFIGURED"
    if active is not None:
        try:
            policy = RetrievalPolicy.model_validate(
                dict(active.retrieval_policy)
            )
            calibration_state = policy.dense_semantic_calibration_state
            serving_fingerprint = runtime.profiles.serving_contract(active)[2]
            if active.reranker_connection_id:
                provider_connection = runtime.control.get_connection(
                    active.reranker_connection_id
                )
                reranker_provider_id = provider_connection.provider_type
                reranker_model = active.reranker_model
        except (RagError, ValueError, KeyError):
            profile_state = "CONFIGURATION_INVALID"
            reasons.append("PROFILE_RUNTIME_CONFIGURATION_INVALID")
        else:
            profile_state = "ACTIVE"
            if (
                revision["index_fingerprint"]
                != active.index_semantic_fingerprint
            ):
                profile_state = "PROFILE_INDEX_MISMATCH"
                reasons.append("PROFILE_INDEX_MISMATCH")
            if runtime.control.profile_validation_issues(
                active.profile_revision_id
            ):
                reasons.append("PROFILE_VALIDATION_STALE")
            if calibration_state == "UNCALIBRATED":
                reasons.append("DENSE_CALIBRATION_MISSING")
    elif latest_draft is not None:
        if activation_state in {"queued", "running"}:
            profile_state = "REBUILD_PENDING"
            reasons.append("PROFILE_REBUILD_PENDING")
        elif activation_state and activation_state.startswith("failed"):
            profile_state = "REBUILD_FAILED"
            reasons.append("PROFILE_REBUILD_FAILED")
        else:
            profile_state = "DRAFT_NOT_ACTIVE"
            reasons.append("PROFILE_DRAFT_NOT_ACTIVE")
    return {
        "retrieval_data_plane": (
            "active_remote_profile"
            if active is not None
            else "default_local_fallback"
        ),
        "profile_state": profile_state,
        "active_retrieval_profile_revision_id": (
            None if active is None else active.profile_revision_id
        ),
        "pending_profile_revision_id": (
            None if latest_draft is None else latest_draft.profile_revision_id
        ),
        "activation_job_id": (
            None if latest_draft is None else latest_draft.activation_job_id
        ),
        "active_index_revision_id": revision["active_revision_id"],
        "index_fingerprint": revision["index_fingerprint"],
        "serving_fingerprint": serving_fingerprint,
        "embedding_provider_id": (
            None if primary is None else primary.provider_id
        ),
        "embedding_model": None if primary is None else primary.model,
        "selected_vector_space": (
            None if primary is None else primary.vector_space_identity
        ),
        "reranker_provider_id": reranker_provider_id,
        "reranker_model": reranker_model,
        "dense_calibration_state": calibration_state,
        "vector_coverage_complete": coverage_complete,
        "fallback_reason_codes": tuple(dict.fromkeys(reasons)),
        "remediation_path": "/retrieval-profiles",
    }


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
