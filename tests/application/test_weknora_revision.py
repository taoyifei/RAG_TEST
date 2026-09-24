"""候选分块在独立 revision 中完成写入、激活与父级回读。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p06_runtime import build_p06_runtime
from rag_app.composition.profiles import profile_from_mapping
from rag_app.core.errors import IndexCorrupt, ValidationFailed
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    DocumentRef,
    KnowledgeBaseScope,
    RetrievalPolicy,
    WeKnoraChunkingPolicy,
)
from tests.adapters.parsers.docx_fixtures import build_docx

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def test_go_parent_child_revision_activates_and_reads_parent(
    tmp_path: Path,
) -> None:
    profile_path = (
        Path(__file__).resolve().parents[2]
        / "configs/profiles/dev-p06-memory.json"
    )
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["profile_id"] = "dev-weknora-memory"
    payload["components"]["chunker"] = "weknora-adaptive-parent-child-v1"
    profile = profile_from_mapping(payload)
    assert isinstance(profile.chunking, WeKnoraChunkingPolicy)

    with build_p06_runtime(profile, data_dir=tmp_path) as runtime:
        project_id = deterministic_id("prj", "weknora-revision")
        kb_id = deterministic_id("kb", project_id, "weknora-revision")
        document_id = deterministic_id("doc", kb_id, "sample")
        runtime.control.put_project(project_id, "Project")
        runtime.control.put_knowledge_base(
            kb_id, project_id, "KB", profile_id=profile.profile_id
        )
        text = "信息安全事件应先报告疑似情况，再报告确认情况。" * 30
        content = build_docx(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")
        document = IngestionDocument(
            document=DocumentRef(
                project_id=project_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                display_name="sample.docx",
            ),
            content=content,
            media_type=_MEDIA_TYPE,
        )

        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=kb_id,
            documents=(document,),
            idempotency_key="weknora-revision",
            budgets=runtime.default_budgets(),
        )
        chunks = runtime.control.chunk_rows(result.revision_id)
        parents = runtime.control.parent_rows(result.revision_id)
        assert result.chunk_count == len(chunks) > 1
        assert parents
        assert {chunk.parent_passage_id for chunk in chunks} == {
            parent.parent_passage_id for parent in parents
        }
        snapshot = runtime.control.active_query_snapshot(
            KnowledgeBaseScope(project_id=project_id, knowledge_base_id=kb_id),
            serving_fingerprint=runtime.components.serving_fingerprint,
            retrieval_policy=RetrievalPolicy(),
        )
        assert snapshot.chunker_id == "weknora-adaptive-parent-child-v1"
        loaded = runtime.control.load_parent_passages(
            snapshot, tuple(parent.parent_passage_id for parent in parents)
        )
        assert loaded == parents

        with runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM parent_passages WHERE revision_id=?",
                (result.revision_id,),
            )
        with pytest.raises(IndexCorrupt):
            runtime.control.load_parent_passages(
                snapshot,
                (parents[0].parent_passage_id,),
            )
        with pytest.raises(ValidationFailed, match="父级"):
            runtime.validator.validate(
                runtime.control.revision_vector_spec(result.revision_id),
                current_index_fingerprint=runtime.components.index_fingerprint,
            )
