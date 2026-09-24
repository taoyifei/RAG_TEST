"""来源节点精确回读的离线索引边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.errors import IndexCorrupt
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    DocumentRef,
    KnowledgeBaseScope,
    RetrievalPolicy,
)
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _document(
    project_id: str,
    knowledge_base_id: str,
    name: str,
    body: str,
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "source-reader", name),
            display_name=f"{name}.docx",
        ),
        content=build_docx(
            f"<w:p><w:r><w:t>{body}</w:t></w:r></w:p>"
        ),
        media_type=_MEDIA_TYPE,
    )


def test_source_reader_keeps_revision_document_and_node_scope(
    tmp_path: Path,
) -> None:
    """原始 node 命中只返回同一活动 revision 的成对文档版本。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    documents = (
        _document(
            project_id,
            knowledge_base_id,
            "甲",
            "甲文档的合成事实。" * 200,
        ),
        _document(project_id, knowledge_base_id, "乙", "乙文档的合成事实。"),
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=documents,
            idempotency_key="source-reader-document-scope",
            budgets=runtime.default_budgets(),
        )
        snapshot = runtime.control.active_query_snapshot(
            KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            ),
            serving_fingerprint=runtime.components.serving_fingerprint,
            retrieval_policy=RetrievalPolicy(),
        )
        ir_by_document = {
            ir.document.document_id: ir
            for ir, _, _ in runtime.control.parse_rows(result.revision_id)
        }
        chunks_by_document = {
            document.document.document_id: tuple(
                chunk
                for chunk in runtime.control.chunk_rows(result.revision_id)
                if chunk.version.document_id == document.document.document_id
            )
            for document in documents
        }
        first, second = (
            ir_by_document[document.document.document_id]
            for document in documents
        )
        first_node_id = next(
            span.node_id
            for chunk in chunks_by_document[first.document.document_id]
            for span in chunk.source_spans
            if span.node_id is not None
        )
        expected = {
            chunk.chunk_id
            for chunk in chunks_by_document[first.document.document_id]
            if any(
                span.node_id == first_node_id for span in chunk.source_spans
            )
        }

        loaded = runtime.control.load_document_ir(
            snapshot,
            first.version,
            max_bytes=1024 * 1024,
        )
        assert loaded == first
        assert (
            runtime.control.load_document_ir(
                snapshot,
                first.version,
                max_bytes=1,
            )
            is None
        )
        matching = runtime.control.source_node_chunk_ids(
            snapshot,
            first.version,
            node_ids=(first_node_id,),
            limit=200,
        )
        assert matching is not None
        assert set(matching) == expected
        assert len(matching) > 1
        assert runtime.control.source_node_chunk_ids(
            snapshot,
            first.version,
            node_ids=(first_node_id,),
            limit=1,
        ) is None
        assert runtime.control.source_node_chunk_ids(
            snapshot,
            second.version,
            node_ids=(first_node_id,),
            limit=200,
        ) == ()
        assert matching == runtime.control.source_node_chunk_ids(
            snapshot,
            first.version,
            node_ids=(first_node_id,),
            limit=200,
        )
        with pytest.raises(IndexCorrupt):
            runtime.control.load_document_ir(
                snapshot,
                first.version.model_copy(
                    update={
                        "document_version_id": (
                            second.version.document_version_id
                        )
                    }
                ),
                max_bytes=1024 * 1024,
            )
    finally:
        runtime.close()


def test_source_reader_rejects_unbounded_or_invalid_input(
    tmp_path: Path,
) -> None:
    """参数在 SQL 查询之前拒绝无界请求和非规范节点身份。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _document(
        project_id, knowledge_base_id, "边界", "合成边界文本。"
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(document,),
            idempotency_key="source-reader-input-boundary",
            budgets=runtime.default_budgets(),
        )
        snapshot = runtime.control.active_query_snapshot(
            KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            ),
            serving_fingerprint=runtime.components.serving_fingerprint,
            retrieval_policy=RetrievalPolicy(),
        )
        version = runtime.control.parse_rows(result.revision_id)[0][0].version
        with pytest.raises(ValueError):
            runtime.control.load_document_ir(
                snapshot, version, max_bytes=0
            )
        with pytest.raises(ValueError):
            runtime.control.source_node_chunk_ids(
                snapshot, version, node_ids=("foreign",), limit=1
            )
        with pytest.raises(ValueError):
            runtime.control.source_node_chunk_ids(
                snapshot,
                version,
                node_ids=("node_" + "0" * 32,) * 65,
                limit=1,
            )
        with pytest.raises(ValueError):
            runtime.control.source_node_chunk_ids(
                snapshot, version, node_ids=(), limit=0
            )
    finally:
        runtime.close()
