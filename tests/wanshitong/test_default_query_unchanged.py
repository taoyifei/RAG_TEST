"""WB-06 默认全量检索与正文身份不变回归。"""

from __future__ import annotations

import json
from time import monotonic, sleep

from rag_app.adapters.chunkers import DocxStructuralChunker
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import ChunkingContext, Job
from rag_app.core.models.common import freeze_json_object
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.upload_validation import DOCX_MEDIA_TYPE
from tests.adapters.parsers.docx.fixtures import build_package, parse_package
from tests.wanshitong.support import PublicHarness


def _docx(text: str) -> bytes:
    return build_package(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")


def _wait_for_job(harness: PublicHarness, job_id: str) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        job = harness.product.runtime.sdk.get_job(job_id)
        if job.state.value not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job_id}")


def test_document_metadata_does_not_change_chunk_identity_or_text() -> None:
    document_ir = parse_package(
        _docx("WB06CHUNKIDENTITY 合成正文保持完全不变。"),
        name="identity.docx",
    ).document_ir
    metadata = freeze_json_object(
        {
            "department_key": "01-科管-94102e0b95",
            "department_name": "01 科管",
            "category_path": ["02 科研项目管理"],
            "document_title": "合成制度",
            "source_relative_path": "01 科管/02 科研项目管理/合成制度.docx",
            "topic_keys": ["policy"],
            "visibility_scope": "all_internal",
            "allowed_roles": [],
            "allowed_groups": [],
            "metadata_revision": "wanshitong-document-metadata-v1",
        }
    )
    enriched_ir = document_ir.model_copy(
        update={
            "metadata": freeze_json_object(
                {**dict(document_ir.metadata), **dict(metadata)}
            )
        }
    )
    chunker = DocxStructuralChunker()
    context = ChunkingContext(
        chunker_fingerprint=chunker.fingerprint,
        index_revision_id=deterministic_id(
            "irev", document_ir.version.document_version_id, "wb-06"
        ),
    )

    before = chunker.chunk(document_ir, context)
    after = chunker.chunk(enriched_ir, context)

    assert len(before.chunks) == len(after.chunks)
    assert tuple(
        chunk.model_dump(mode="json", exclude={"metadata"})
        for chunk in before.chunks
    ) == tuple(
        chunk.model_dump(mode="json", exclude={"metadata"})
        for chunk in after.chunks
    )
    for before_chunk, after_chunk in zip(
        before.chunks, after.chunks, strict=True
    ):
        before_metadata = dict(before_chunk.metadata)
        after_metadata = dict(after_chunk.metadata)
        assert {
            key: after_metadata[key] for key in before_metadata
        } == before_metadata
        assert {key: after_metadata[key] for key, _ in metadata} == dict(
            metadata
        )
    assert all(
        "01 科管" not in chunk.embedding_text
        and "02 科研项目管理" not in chunk.lexical_text
        for chunk in after.chunks
    )


def test_default_search_keeps_documents_from_different_departments(
    public_harness: PublicHarness,
) -> None:
    document_ids: set[str] = set()
    for number, department in enumerate(("第一部门", "第二部门"), start=1):
        metadata = {
            "source_relative_path": f"{department}/分类/资料{number}.docx",
            "department_name": department,
            "category_path": ["分类"],
            "topic_keys": [f"topic-{number}"],
        }
        response = public_harness.client.post(
            ADMIN_BASE_PATH + "/documents",
            params={"metadata": json.dumps(metadata, ensure_ascii=False)},
            headers={
                **public_harness.product.write_headers,
                "Content-Type": DOCX_MEDIA_TYPE,
                "Idempotency-Key": f"default-full-search-{number}",
            },
            content=_docx(
                f"WB06FULLSEARCHTOKEN 这是{department}的合成检索内容。"
            ),
        )
        assert response.status_code == 202
        payload = response.json()
        document_ids.add(str(payload["document"]["document_id"]))
        job = _wait_for_job(public_harness, payload["job"]["job_id"])
        assert job.state.value == "succeeded"

    binding = public_harness.scope_service.binding()
    result = public_harness.product.runtime.sdk.search(
        binding.project_id,
        binding.knowledge_base_id,
        "WB06FULLSEARCHTOKEN",
        limit=10,
        owner_id="wanshitong-default-full-search-test",
    )
    assert result.diagnostics is not None
    lexical_chunk_ids = set(
        dict(result.diagnostics.channel_chunk_ids).get("lexical", ())
    )
    with public_harness.product.runtime.connections.transaction() as connection:
        rows = connection.execute(
            "SELECT chunk_id, document_id FROM chunks "
            "WHERE revision_id=? ORDER BY chunk_id",
            (result.active_index_revision_id,),
        ).fetchall()
    matching_chunk_ids = {
        str(row["chunk_id"])
        for row in rows
        if str(row["document_id"]) in document_ids
    }

    assert matching_chunk_ids
    assert {
        str(row["document_id"])
        for row in rows
        if str(row["chunk_id"]) in lexical_chunk_ids
    } == document_ids
    assert matching_chunk_ids.issubset(lexical_chunk_ids)
