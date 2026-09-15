"""固定 Scope、DOCX-only 的湾事通管理员薄 Facade。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Annotated, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import NotFound, PolicyDenied
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import Document, DocumentRef, Job
from rag_app.product.models import ProviderConnection
from rag_app.tracing.models import TraceListFilter
from rag_app.tracing.store import (
    ArtifactExpiredError,
    ArtifactNotFoundError,
    TraceNotFoundError,
)
from rag_app.wanshitong.admin_models import (
    WanshitongDocumentPage,
    WanshitongDocumentView,
    WanshitongTraceQuery,
    WanshitongUploadReceipt,
)
from rag_app.wanshitong.document_metadata import (
    WanshitongDocumentMetadata,
    WanshitongDocumentMetadataStore,
)
from rag_app.wanshitong.errors import AdminFacadeError, ScopeBindingError
from rag_app.wanshitong.models import ScopeBinding
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.upload_validation import (
    ValidatedDocxUpload,
    read_and_validate_docx,
)

ADMIN_BASE_PATH = "/api/v1/admin/wanshitong"
_ADMIN_PRINCIPALS = frozenset({"admin_session", "legacy_admin"})


def register_admin_routes(
    app: FastAPI,
    *,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
    document_metadata: WanshitongDocumentMetadataStore,
) -> None:
    """注册复用唯一 Product Runtime 的管理员 Facade。"""
    _register_error_handler(app)
    _register_document_routes(
        app, runtime, scope_service, document_metadata
    )
    _register_job_routes(app, runtime, scope_service)
    _register_history_routes(app, runtime, scope_service)
    _register_trace_routes(app, runtime, scope_service)
    _register_status_routes(app, runtime, scope_service, document_metadata)


def _register_error_handler(app: FastAPI) -> None:
    @app.exception_handler(AdminFacadeError)
    async def _admin_error(
        request: Request, error: AdminFacadeError
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "stage": error.stage,
                    "retryable": False,
                    "trace_id": "",
                    "details": {},
                }
            },
        )


def _register_document_routes(
    app: FastAPI,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
    metadata_store: WanshitongDocumentMetadataStore,
) -> None:
    @app.get(
        ADMIN_BASE_PATH + "/documents",
        tags=["wanshitong-admin"],
        response_model=WanshitongDocumentPage,
    )
    def _documents(
        request: Request,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        cursor: Annotated[
            str | None, Query(max_length=20, pattern=r"^[0-9]+$")
        ] = None,
    ) -> WanshitongDocumentPage:
        binding = _admin_scope(request, scope_service)
        return _document_page(
            runtime,
            metadata_store,
            binding,
            page_size=page_size,
            offset=_resolved_offset(cursor, offset),
        )

    @app.post(
        ADMIN_BASE_PATH + "/documents",
        tags=["wanshitong-admin"],
        response_model=WanshitongUploadReceipt,
        status_code=202,
    )
    async def _upload_document(
        request: Request,
        relative_path: Annotated[str, Query(min_length=1, max_length=4096)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
    ) -> WanshitongUploadReceipt:
        binding = _admin_scope(request, scope_service)
        proposed_id = deterministic_id(
            "doc",
            binding.project_id,
            binding.knowledge_base_id,
            idempotency_key,
        )
        upload = await _validated_upload(
            request,
            runtime,
            binding,
            document_id=proposed_id,
            relative_path=relative_path,
        )
        job = runtime.sdk.create_document(
            binding.project_id,
            binding.knowledge_base_id,
            display_name=upload.display_name,
            content=upload.content,
            media_type=upload.media_type,
            idempotency_key=idempotency_key,
        )
        document = _job_document(runtime, binding, job)
        department, category_path = _path_metadata(upload.relative_path)
        metadata = metadata_store.bind(
            document=document,
            relative_path=upload.relative_path,
            department=department,
            category_path=category_path,
        )
        return WanshitongUploadReceipt(
            document=_document_view(runtime, document, metadata, job),
            job=job,
        )

    @app.get(
        ADMIN_BASE_PATH + "/documents/{document_id}",
        tags=["wanshitong-admin"],
        response_model=WanshitongDocumentView,
    )
    def _document(
        document_id: str, request: Request
    ) -> WanshitongDocumentView:
        binding = _admin_scope(request, scope_service)
        document = runtime.sdk.get_document(
            binding.project_id, binding.knowledge_base_id, document_id
        )
        return _document_view(
            runtime,
            document,
            metadata_store.get(document_id),
            _latest_job(runtime, binding, document_id),
        )

    @app.post(
        ADMIN_BASE_PATH + "/documents/{document_id}/versions",
        tags=["wanshitong-admin"],
        response_model=WanshitongUploadReceipt,
        status_code=202,
    )
    async def _upload_version(
        document_id: str,
        request: Request,
        relative_path: Annotated[str, Query(min_length=1, max_length=4096)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
    ) -> WanshitongUploadReceipt:
        binding = _admin_scope(request, scope_service)
        document = runtime.sdk.get_document(
            binding.project_id, binding.knowledge_base_id, document_id
        )
        upload = await _validated_upload(
            request,
            runtime,
            binding,
            document_id=document_id,
            relative_path=relative_path,
        )
        job = runtime.sdk.create_document_version(
            binding.project_id,
            binding.knowledge_base_id,
            document_id,
            content=upload.content,
            media_type=upload.media_type,
            idempotency_key=idempotency_key,
        )
        updated = runtime.sdk.get_document(
            binding.project_id, binding.knowledge_base_id, document_id
        )
        department, category_path = _path_metadata(upload.relative_path)
        metadata = metadata_store.bind(
            document=document,
            relative_path=upload.relative_path,
            department=department,
            category_path=category_path,
        )
        return WanshitongUploadReceipt(
            document=_document_view(runtime, updated, metadata, job), job=job
        )

    @app.delete(
        ADMIN_BASE_PATH + "/documents/{document_id}",
        tags=["wanshitong-admin"],
        status_code=204,
    )
    def _delete_document(document_id: str, request: Request) -> Response:
        binding = _admin_scope(request, scope_service)
        runtime.sdk.get_document(
            binding.project_id, binding.knowledge_base_id, document_id
        )
        runtime.sdk.delete_document(
            binding.project_id, binding.knowledge_base_id, document_id
        )
        return Response(status_code=204)


def _register_job_routes(
    app: FastAPI,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
) -> None:
    @app.get(ADMIN_BASE_PATH + "/jobs", tags=["wanshitong-admin"])
    def _jobs(
        request: Request,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        cursor: Annotated[
            str | None, Query(max_length=20, pattern=r"^[0-9]+$")
        ] = None,
    ) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        resolved_offset = _resolved_offset(cursor, offset)
        page = runtime.sdk.list_jobs(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            page_size=page_size,
            offset=resolved_offset,
        )
        payload = cast(dict[str, object], page.model_dump(mode="json"))
        payload["next_cursor"] = (
            None if page.next_offset is None else str(page.next_offset)
        )
        return payload

    @app.get(
        ADMIN_BASE_PATH + "/jobs/{job_id}", tags=["wanshitong-admin"]
    )
    def _job(job_id: str, request: Request) -> Job:
        binding = _admin_scope(request, scope_service)
        return _scoped_job(runtime, binding, job_id)

    @app.post(
        ADMIN_BASE_PATH + "/jobs/{job_id}:retry",
        tags=["wanshitong-admin"],
    )
    def _retry_job(job_id: str, request: Request) -> Job:
        binding = _admin_scope(request, scope_service)
        _scoped_job(runtime, binding, job_id)
        return runtime.sdk.retry_job(job_id)

    @app.post(
        ADMIN_BASE_PATH + "/jobs/{job_id}:cancel",
        tags=["wanshitong-admin"],
    )
    def _cancel_job(job_id: str, request: Request) -> Job:
        binding = _admin_scope(request, scope_service)
        _scoped_job(runtime, binding, job_id)
        return runtime.sdk.cancel_job(job_id)


def _register_history_routes(
    app: FastAPI,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
) -> None:
    @app.get(ADMIN_BASE_PATH + "/history", tags=["wanshitong-admin"])
    def _history(
        request: Request,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        payload = runtime.history.list_history(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            page_size=page_size,
            offset=offset,
        )
        _add_owner_masks(runtime, payload)
        return payload

    @app.get(
        ADMIN_BASE_PATH + "/history/{trace_id}",
        tags=["wanshitong-admin"],
    )
    def _history_detail(trace_id: str, request: Request) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        runtime.traces.flush()
        payload = runtime.history.detail(
            trace_id,
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        _add_owner_mask(runtime, payload)
        return payload

    @app.delete(ADMIN_BASE_PATH + "/history", tags=["wanshitong-admin"])
    def _clear_history(request: Request) -> dict[str, int]:
        binding = _admin_scope(request, scope_service)
        deleted = runtime.history.clear_scope(
            binding.project_id, binding.knowledge_base_id
        )
        return {"deleted_count": deleted}


def _register_trace_routes(
    app: FastAPI,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
) -> None:
    @app.get(
        ADMIN_BASE_PATH + "/operational-traces",
        tags=["wanshitong-admin"],
    )
    def _traces(
        request: Request,
        filters: Annotated[WanshitongTraceQuery, Query()],
    ) -> object:
        binding = _admin_scope(request, scope_service)
        result = runtime.traces.list_traces(
            TraceListFilter(
                **filters.model_dump(),
                project_id=binding.project_id,
                knowledge_base_id=binding.knowledge_base_id,
            )
        )
        return jsonable_encoder(result)

    @app.get(
        ADMIN_BASE_PATH + "/operational-traces/{trace_id}",
        tags=["wanshitong-admin"],
    )
    def _trace_detail(trace_id: str, request: Request) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        return _scoped_trace_detail(runtime, binding, trace_id)

    @app.get(
        ADMIN_BASE_PATH
        + "/operational-traces/{trace_id}/artifacts/{artifact_id}",
        tags=["wanshitong-admin"],
    )
    def _trace_artifact(
        trace_id: str, artifact_id: str, request: Request
    ) -> Response:
        binding = _admin_scope(request, scope_service)
        detail = _scoped_trace_detail(runtime, binding, trace_id)
        _reauthorize_trace_sources(runtime, detail)
        try:
            artifact = runtime.traces.artifact(trace_id, artifact_id)
        except (ArtifactExpiredError, ArtifactNotFoundError):
            raise AdminFacadeError(
                "NOT_FOUND",
                "Trace Artifact 不存在或已过期。",
                status_code=404,
                stage="wanshitong.trace.artifact",
            ) from None
        return Response(
            artifact.payload,
            media_type=artifact.metadata.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": artifact.metadata.sha256,
            },
        )


def _register_status_routes(
    app: FastAPI,
    runtime: ProductRuntime,
    scope_service: FixedScopeService,
    metadata_store: WanshitongDocumentMetadataStore,
) -> None:
    @app.get(ADMIN_BASE_PATH + "/models", tags=["wanshitong-admin"])
    def _models(request: Request) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        return _model_status(runtime, binding)

    @app.get(ADMIN_BASE_PATH + "/overview", tags=["wanshitong-admin"])
    def _overview(request: Request) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        return _overview_status(runtime, metadata_store, binding)

    @app.get(ADMIN_BASE_PATH + "/system", tags=["wanshitong-admin"])
    def _system(request: Request) -> dict[str, object]:
        binding = _admin_scope(request, scope_service)
        return _system_status(runtime, binding)


async def _validated_upload(
    request: Request,
    runtime: ProductRuntime,
    binding: ScopeBinding,
    *,
    document_id: str,
    relative_path: str,
) -> ValidatedDocxUpload:
    components = runtime.p09.retrieval_runtime.persistence.components
    return await read_and_validate_docx(
        request,
        relative_path=relative_path,
        document=DocumentRef(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            document_id=document_id,
            display_name="pending.docx",
        ),
        parser=components.parser,
        parsing_policy=components.parsing_policy,
    )


def _document_page(
    runtime: ProductRuntime,
    metadata_store: WanshitongDocumentMetadataStore,
    binding: ScopeBinding,
    *,
    page_size: int,
    offset: int,
) -> WanshitongDocumentPage:
    documents = runtime.sdk.list_documents(
        binding.project_id,
        binding.knowledge_base_id,
        limit=page_size,
        offset=offset,
    )
    metadata = metadata_store.list_for_scope(
        binding.project_id, binding.knowledge_base_id
    )
    jobs = runtime.sdk.list_jobs(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        page_size=200,
    ).items
    latest_jobs = _latest_jobs_by_document(jobs)
    total = len(
        runtime.sdk.list_documents(
            binding.project_id, binding.knowledge_base_id, limit=200, offset=0
        )
    )
    candidate_offset = offset + len(documents)
    next_offset = None if candidate_offset >= total else candidate_offset
    return WanshitongDocumentPage(
        items=tuple(
            _document_view(
                runtime,
                document,
                metadata.get(document.document_id),
                latest_jobs.get(document.document_id),
            )
            for document in documents
        ),
        total=total,
        page_size=page_size,
        offset=offset,
        next_offset=next_offset,
        next_cursor=None if next_offset is None else str(next_offset),
    )


def _document_view(
    runtime: ProductRuntime,
    document: Document,
    metadata: WanshitongDocumentMetadata | None,
    latest_job: Job | None,
) -> WanshitongDocumentView:
    version_status = None
    if document.current_version_id is not None:
        version = runtime.sdk.get_document_version(
            document.project_id,
            document.knowledge_base_id,
            document.document_id,
            document.current_version_id,
        )
        version_status = version.status.value
    return WanshitongDocumentView(
        document_id=document.document_id,
        display_name=document.display_name,
        relative_path=None if metadata is None else metadata.relative_path,
        department=None if metadata is None else metadata.department,
        category_path=() if metadata is None else metadata.category_path,
        status=document.status,
        current_version_id=document.current_version_id,
        current_version_status=version_status,
        active_index_revision_id=document.active_index_revision_id,
        latest_job=latest_job,
        retrievable=document.active_index_revision_id is not None,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _job_document(
    runtime: ProductRuntime, binding: ScopeBinding, job: Job
) -> Document:
    if job.document_id is None:
        raise RuntimeError("文档上传 Job 缺少 Document ID。")
    return runtime.sdk.get_document(
        binding.project_id, binding.knowledge_base_id, job.document_id
    )


def _latest_jobs_by_document(jobs: Sequence[Job]) -> dict[str, Job]:
    result: dict[str, Job] = {}
    for job in jobs:
        if job.document_id is not None and job.document_id not in result:
            result[job.document_id] = job
    return result


def _latest_job(
    runtime: ProductRuntime, binding: ScopeBinding, document_id: str
) -> Job | None:
    jobs = runtime.sdk.list_jobs(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        page_size=200,
    ).items
    return _latest_jobs_by_document(jobs).get(document_id)


def _scoped_job(
    runtime: ProductRuntime, binding: ScopeBinding, job_id: str
) -> Job:
    job = runtime.sdk.get_job(job_id)
    if (
        job.project_id != binding.project_id
        or job.knowledge_base_id != binding.knowledge_base_id
    ):
        raise AdminFacadeError(
            "NOT_FOUND",
            "处理任务不存在。",
            status_code=404,
            stage="wanshitong.job.read",
        )
    return job


def _admin_scope(
    request: Request, scope_service: FixedScopeService
) -> ScopeBinding:
    principal = getattr(request.state, "product_principal", None)
    if principal not in _ADMIN_PRINCIPALS:
        raise PolicyDenied(
            "湾事通管理员 Facade 仅允许管理员读取。",
            stage="wanshitong.admin.authorization",
        )
    try:
        return scope_service.binding()
    except ScopeBindingError as error:
        raise AdminFacadeError(
            error.code,
            error.safe_message,
            status_code=409,
            stage="wanshitong.scope.status",
        ) from error


def _add_owner_masks(
    runtime: ProductRuntime, payload: dict[str, object]
) -> None:
    items = payload.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict):
            _add_owner_mask(runtime, item)


def _add_owner_mask(runtime: ProductRuntime, item: dict[str, object]) -> None:
    trace_id = item.get("trace_id")
    if not isinstance(trace_id, str):
        return
    with runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT owner_id FROM query_history WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    if row is None:
        return
    digest = hashlib.sha256(str(row["owner_id"]).encode("utf-8")).hexdigest()
    item["owner_masked_id"] = "anonymous-" + digest[:12]


def _scoped_trace_detail(
    runtime: ProductRuntime, binding: ScopeBinding, trace_id: str
) -> dict[str, object]:
    try:
        payload = runtime.traces.legacy_detail(trace_id)
    except TraceNotFoundError:
        raise AdminFacadeError(
            "NOT_FOUND",
            "Operational Trace 不存在。",
            status_code=404,
            stage="wanshitong.trace.read",
        ) from None
    trace = payload.get("trace")
    if not isinstance(trace, Mapping) or (
        trace.get("project_id") != binding.project_id
        or trace.get("knowledge_base_id") != binding.knowledge_base_id
    ):
        raise AdminFacadeError(
            "NOT_FOUND",
            "Operational Trace 不存在。",
            status_code=404,
            stage="wanshitong.trace.read",
        )
    return payload


def _reauthorize_trace_sources(
    runtime: ProductRuntime, detail: Mapping[str, object]
) -> None:
    root = detail.get("trace")
    if not isinstance(root, Mapping):
        raise PolicyDenied("Trace 根身份无效。", stage="trace.scope")
    project_id = root.get("project_id")
    knowledge_base_id = root.get("knowledge_base_id")
    if not isinstance(project_id, str) or not isinstance(
        knowledge_base_id, str
    ):
        return
    try:
        runtime.sdk.get_knowledge_base(project_id, knowledge_base_id)
        document_id = root.get("document_id")
        if isinstance(document_id, str):
            runtime.sdk.get_document(
                project_id, knowledge_base_id, document_id
            )
    except NotFound as error:
        raise PolicyDenied(
            "Trace 来源已删除或无权读取。", stage="trace.source"
        ) from error


def _model_status(
    runtime: ProductRuntime, binding: ScopeBinding
) -> dict[str, object]:
    profile = runtime.control.active_profile(binding.knowledge_base_id)
    settings = runtime.models.get(binding.knowledge_base_id)
    embedding = _connection_status(
        runtime,
        None if profile is None else profile.primary_connection_id,
        None if profile is None else profile.primary_embedding_model,
        dimension=None if profile is None else profile.primary_dimension,
    )
    reranker = _connection_status(
        runtime,
        None if profile is None else profile.reranker_connection_id,
        None if profile is None else profile.reranker_model,
    )
    llm = _connection_status(
        runtime,
        settings.generation_connection_id,
        settings.generation_model,
    )
    ocr = _connection_status(
        runtime, settings.ocr_connection_id, settings.ocr_model
    )
    pdf_parser = _connection_status(
        runtime,
        settings.pdf_parser_connection_id,
        settings.pdf_parser_model,
    )
    ocr.update({"enabled": settings.ocr_enabled})
    pdf_parser.update({"enabled": settings.pdf_parser_enabled})
    return {
        "embedding": embedding,
        "reranker": reranker,
        "llm": llm,
        "retrieval_profile": None
        if profile is None
        else {
            "profile_revision_id": profile.profile_revision_id,
            "status": profile.status,
            "index_semantic_fingerprint": profile.index_semantic_fingerprint,
            "serving_fingerprint": profile.serving_fingerprint,
        },
        "kb_model_settings": {
            "generation_connection_id": settings.generation_connection_id,
            "generation_model": settings.generation_model,
            "rewrite_enabled": settings.rewrite_enabled,
            "ocr_connection_id": settings.ocr_connection_id,
            "ocr_model": settings.ocr_model,
            "ocr_enabled": settings.ocr_enabled,
            "pdf_parser_connection_id": settings.pdf_parser_connection_id,
            "pdf_parser_model": settings.pdf_parser_model,
            "pdf_parser_enabled": settings.pdf_parser_enabled,
        },
        "image_ocr": ocr,
        "pdf_parser": pdf_parser,
    }


def _connection_status(
    runtime: ProductRuntime,
    connection_id: str | None,
    model: str | None,
    *,
    dimension: int | None = None,
) -> dict[str, object]:
    if connection_id is None or model is None:
        return {"configured": False, "model": model, "dimension": dimension}
    connection = runtime.control.get_connection(connection_id)
    validations = runtime.control.list_validations(connection_id)
    validation = validations[0] if validations else None
    return {
        "configured": True,
        "connection_id": connection.connection_id,
        "connection_name": connection.display_name,
        "provider_type": connection.provider_type,
        "model": model,
        "dimension": dimension,
        "protocol": connection.rerank_protocol,
        "path": connection.rerank_path,
        "host": _masked_host(connection),
        "status": connection.status,
        "validation_status": None
        if validation is None
        else validation.status,
        "validated_at": None
        if validation is None
        else validation.finished_at,
    }


def _masked_host(connection: ProviderConnection) -> str | None:
    raw = connection.api_base_url or connection.api_host
    if raw is None:
        return None
    parsed = urlsplit(raw if "://" in raw else "//" + raw)
    return parsed.hostname


def _overview_status(
    runtime: ProductRuntime,
    metadata_store: WanshitongDocumentMetadataStore,
    binding: ScopeBinding,
) -> dict[str, object]:
    scope = runtime.sdk.get_knowledge_base(
        binding.project_id, binding.knowledge_base_id
    )
    documents = _document_page(
        runtime, metadata_store, binding, page_size=200, offset=0
    ).items
    job_page = runtime.sdk.list_jobs(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        page_size=200,
    )
    counts = {
        state: sum(job.state.value == state for job in job_page.items)
        for state in (
            "queued",
            "running",
            "failed_retryable",
            "failed_terminal",
        )
    }
    model_status = _model_status(runtime, binding)
    recent_history = runtime.history.list_history(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        page_size=5,
    )
    _add_owner_masks(runtime, recent_history)
    return {
        "scope": {
            "mode": "wanshitong",
            "ready": True,
            "system_key": binding.system_key,
            "project_id": binding.project_id,
            "project_name": "湾事通",
            "knowledge_base_id": binding.knowledge_base_id,
            "knowledge_base_name": scope.name,
            "created_at": binding.created_at,
            "blocker_code": None,
            "blocker_message": None,
        },
        "documents": {
            "total": len(documents),
            "retrievable": sum(item.retrievable for item in documents),
        },
        "jobs": counts,
        "active_index_revision_id": scope.active_index_revision_id,
        "providers": {
            name: model_status[name]
            for name in ("embedding", "reranker", "llm")
        },
        "public_query_ready": bool(
            scope.active_index_revision_id
            and cast(dict[str, object], model_status["embedding"])[
                "configured"
            ]
            and cast(dict[str, object], model_status["llm"])["configured"]
        ),
        "supported_formats": {
            "docx": "enabled",
            "pdf": "disabled",
            "doc": "disabled",
            "excel": "disabled",
            "zip": "disabled",
        },
        "history_retention_days": runtime.history.retention_days,
        "recent_history": recent_history["items"],
        "recent_errors": _recent_job_errors(runtime, job_page.items),
    }


def _system_status(
    runtime: ProductRuntime, binding: ScopeBinding
) -> dict[str, object]:
    with runtime.connections.transaction() as connection:
        sqlite_ready = connection.execute("SELECT 1").fetchone() is not None
    knowledge_base = runtime.sdk.get_knowledge_base(
        binding.project_id, binding.knowledge_base_id
    )
    health = runtime.sdk.health()
    executor = runtime.p09.query_executor
    return {
        "mode": "wanshitong",
        "app": {"status": "live"},
        "live": True,
        "ready": sqlite_ready,
        "qdrant": {
            "mode": runtime.settings.qdrant_mode,
            "status": "ready"
            if runtime.settings.qdrant_mode == "memory"
            else "configured_not_probed",
        },
        "sqlite": {
            "ready": sqlite_ready,
            "integrity_status": health.integrity_status,
        },
        "scope": {
            "ready": True,
            "project_id": binding.project_id,
            "knowledge_base_id": binding.knowledge_base_id,
        },
        "active_index_revision_id": knowledge_base.active_index_revision_id,
        "query_executor": {
            "max_workers": executor.max_workers,
            "max_queue": executor.max_queue,
            "in_flight": executor.in_flight,
            "retry_after_seconds": executor.retry_after_seconds,
        },
        "history": {
            "ready": True,
            "retention_days": runtime.history.retention_days,
        },
        "operational_trace": {
            "ready": True,
            "metrics": runtime.traces.metrics(),
        },
        "public_session": {"ready": True},
        "frontend_build_id": runtime.compatibility.frontend_build_id,
    }


def _path_metadata(
    relative_path: str,
) -> tuple[str | None, tuple[str, ...]]:
    parts = relative_path.split("/")
    if len(parts) == 1:
        return None, ()
    return parts[0], tuple(parts[1:-1])


def _resolved_offset(cursor: str | None, offset: int) -> int:
    if cursor is None:
        return offset
    cursor_offset = int(cursor)
    if offset not in {0, cursor_offset}:
        raise AdminFacadeError(
            "INVALID_CURSOR",
            "分页 cursor 与 offset 不一致。",
            stage="wanshitong.pagination",
        )
    return cursor_offset


def _recent_job_errors(
    runtime: ProductRuntime, jobs: Sequence[Job]
) -> list[dict[str, object]]:
    failures = [
        job
        for job in jobs
        if job.state.value in {"failed_retryable", "failed_terminal"}
    ][:5]
    result: list[dict[str, object]] = []
    with runtime.connections.transaction() as connection:
        for job in failures:
            row = connection.execute(
                "SELECT updated_at FROM ingestion_jobs WHERE job_id=?",
                (job.job_id,),
            ).fetchone()
            result.append(
                {
                    "code": job.error_code or "JOB_FAILED",
                    "message": job.safe_error or "文档处理任务失败。",
                    "occurred_at": None
                    if row is None
                    else str(row["updated_at"]),
                    "job_id": job.job_id,
                    "document_id": job.document_id,
                }
            )
    return result


__all__ = ["ADMIN_BASE_PATH", "register_admin_routes"]
