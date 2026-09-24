"""ContextReader 在真实离线索引上的来源完整性测试。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from rag_app.adapters.stores.sqlite_control import SqliteControlStore
from rag_app.application.retrieval.context_reader import ContextReader
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p06_runtime import P06Runtime
from rag_app.core.errors import IndexCorrupt
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    Chunk,
    ChunkRole,
    DocumentIR,
    DocumentRef,
    DocumentVersionRef,
    HydratedChunk,
    KnowledgeBaseScope,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.ports import EvidenceSourcePort
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@dataclass(frozen=True)
class _TableScenario:
    """真实合成索引及目标行命中。"""

    runtime: P06Runtime
    snapshot: ActiveRevisionQuerySnapshot
    policy: RetrievalPolicy
    document_ir: DocumentIR
    seed: RankedChunk


class _OmittingSource:
    """模拟同版本来源查询漏回一个 canonical Chunk。"""

    def __init__(self, source: SqliteControlStore, omitted_id: str) -> None:
        self._source = source
        self._omitted_id = omitted_id

    def load_document_ir(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        document_version: DocumentVersionRef,
        *,
        max_bytes: int,
    ) -> DocumentIR | None:
        return self._source.load_document_ir(
            snapshot, document_version, max_bytes=max_bytes
        )

    def source_node_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        document_version: DocumentVersionRef,
        *,
        node_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[str, ...] | None:
        ids = self._source.source_node_chunk_ids(
            snapshot, document_version, node_ids=node_ids, limit=limit
        )
        return (
            None
            if ids is None
            else tuple(
                chunk_id
                for chunk_id in ids
                if chunk_id != self._omitted_id
            )
        )

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        return self._source.hydrate_chunks(snapshot, chunk_ids)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _table_document(
    project_id: str, knowledge_base_id: str
) -> IngestionDocument:
    owner_text = "甲方分阶段书面报送核对记录，乙方检查签收情况。" * 4
    table = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc>"
        + _paragraph("事项")
        + "</w:tc><w:tc>"
        + _paragraph("处理要求")
        + "</w:tc></w:tr><w:tr><w:tc>"
        '<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        + _paragraph(owner_text)
        + "</w:tc><w:tc>"
        + _paragraph("初步核对。")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + "<w:tcPr><w:vMerge/></w:tcPr><w:p/>"
        + "</w:tc><w:tc>"
        + _paragraph("最终复核。")
        + "</w:tc></w:tr></w:tbl>"
    )
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "context-reader", "table"),
            display_name="合成表格.docx",
        ),
        content=build_docx(table),
        media_type=_MEDIA_TYPE,
    )


def _is_target_row_chunk(chunk: Chunk) -> bool:
    """只选真实 atom 标注为目标逻辑行的表格 Chunk。"""
    if chunk.role is not ChunkRole.TABLE:
        return False
    atoms = dict(chunk.metadata).get("atoms")
    if not isinstance(atoms, (list, tuple)):
        return False
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        metadata = atom.get("metadata")
        if isinstance(metadata, dict) and metadata.get("row_index") == 2:
            return True
    return False


@pytest.fixture
def table_scenario(
    tmp_path: Path,
) -> Iterator[_TableScenario]:
    """构建活动 revision，选择物理单元格继承的目标逻辑行。"""
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = _table_document(project_id, knowledge_base_id)
    try:
        built = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(document,),
            idempotency_key="context-reader-vertical-merge",
            budgets=runtime.default_budgets(),
        )
        policy = RetrievalPolicy()
        snapshot = runtime.control.active_query_snapshot(
            KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            ),
            serving_fingerprint=runtime.components.serving_fingerprint,
            retrieval_policy=policy,
        )
        chunks = runtime.control.chunk_rows(built.revision_id)
        row_chunks = tuple(
            chunk
            for chunk in chunks
            if _is_target_row_chunk(chunk)
        )
        assert len(row_chunks) >= 2
        seed = RankedChunk(
            hydrated=HydratedChunk(
                chunk=row_chunks[0], display_name="合成表格.docx"
            ),
            fusion_rank=1,
        )
        yield _TableScenario(
            runtime=runtime,
            snapshot=snapshot,
            policy=policy,
            document_ir=runtime.control.parse_rows(built.revision_id)[0][0],
            seed=seed,
        )
    finally:
        runtime.close()


def test_context_reader_recovers_vertical_owner_for_logical_row(
    table_scenario: _TableScenario,
) -> None:
    """长合并单元格的原节点必须完整进入目标逻辑行。"""
    scenario = table_scenario
    owner = next(
        node
        for node in scenario.document_ir.nodes
        if node.text_payload is not None
        and "甲方分阶段书面报送" in node.text_payload.exact_text
    )
    read = ContextReader(scenario.runtime.control).read(
        scenario.snapshot, (scenario.seed,), scenario.policy
    )

    assert read.skipped_seed_ids == ()
    group = next(
        group
        for group in read.groups
        if group.kind == "table_row" and group.table_row_index == 2
    )
    assert group.source_complete
    assert group.reason_codes == ()
    assert owner.node_id in group.required_node_ids
    assert group.missing_node_ids == ()
    owner_pieces = tuple(
        piece for piece in group.pieces if piece.span.node_id == owner.node_id
    )
    assert owner.text_payload is not None
    assert any(piece.span.is_repeated for piece in owner_pieces)
    assert {
        index
        for piece in owner_pieces
        for index in range(
            piece.span.source_start_char or 0,
            piece.span.source_end_char or 0,
        )
    } == set(range(len(owner.text_payload.exact_text)))
    assert all(
        piece.text
        == owner.text_payload.exact_text[
            piece.span.source_start_char : piece.span.source_end_char
        ]
        for piece in owner_pieces
    )


def test_context_reader_retains_partial_group_when_source_chunk_is_missing(
    table_scenario: _TableScenario,
) -> None:
    """真实来源片段缺失时保留可读部分并显式标明缺段。"""
    scenario = table_scenario
    full = ContextReader(scenario.runtime.control).read(
        scenario.snapshot, (scenario.seed,), scenario.policy
    )
    group = next(group for group in full.groups if group.table_row_index == 2)
    owner_id = next(
        node.node_id
        for node in scenario.document_ir.nodes
        if node.text_payload is not None
        and "甲方分阶段书面报送" in node.text_payload.exact_text
    )
    owner = next(
        node for node in scenario.document_ir.nodes if node.node_id == owner_id
    )
    assert owner.text_payload is not None
    omitted_id = next(
        chunk_id
        for chunk_id in (
            piece.candidate.hydrated.chunk.chunk_id
            for piece in group.pieces
            if piece.span.node_id == owner_id
        )
        if len(
            {
                index
                for piece in group.pieces
                if piece.span.node_id == owner_id
                and piece.candidate.hydrated.chunk.chunk_id != chunk_id
                for index in range(
                    piece.span.source_start_char or 0,
                    piece.span.source_end_char or 0,
                )
            }
        ) < len(owner.text_payload.exact_text)
    )
    source = cast(
        EvidenceSourcePort,
        _OmittingSource(scenario.runtime.control, omitted_id),
    )
    partial = ContextReader(source).read(
        scenario.snapshot, (scenario.seed,), scenario.policy
    )

    partial_group = next(
        item for item in partial.groups if item.table_row_index == 2
    )
    assert partial_group.pieces
    assert not partial_group.source_complete
    assert owner_id in partial_group.missing_node_ids
    assert "SOURCE_NODE_INCOMPLETE" in partial_group.reason_codes


def test_context_reader_rejects_foreign_scope_and_version(
    table_scenario: _TableScenario,
) -> None:
    """伪造项目或版本的 seed 不能借用活动索引中的来源。"""
    scenario = table_scenario
    original = scenario.seed.hydrated.chunk
    foreign_scope_chunk = original.model_copy(
        update={"project_id": f"prj_{'f' * 32}"}
    )
    foreign_scope_seed = scenario.seed.model_copy(
        update={
            "hydrated": scenario.seed.hydrated.model_copy(
                update={"chunk": foreign_scope_chunk}
            )
        }
    )
    rejected = ContextReader(scenario.runtime.control).read(
        scenario.snapshot, (foreign_scope_seed,), scenario.policy
    )
    assert rejected.groups == ()
    assert rejected.skipped_seed_ids == (
        (original.chunk_id, "SOURCE_SCOPE_MISMATCH"),
    )

    foreign_version_chunk = original.model_copy(
        update={
            "version": original.version.model_copy(
                update={"document_version_id": f"dver_{'f' * 32}"}
            )
        }
    )
    foreign_version_seed = scenario.seed.model_copy(
        update={
            "hydrated": scenario.seed.hydrated.model_copy(
                update={"chunk": foreign_version_chunk}
            )
        }
    )
    with pytest.raises(IndexCorrupt):
        ContextReader(scenario.runtime.control).read(
            scenario.snapshot, (foreign_version_seed,), scenario.policy
        )
