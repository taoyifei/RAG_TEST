"""`docx-structural-v3` 同步、离线 Chunker adapter。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence

from pydantic import JsonValue

from rag_app.adapters.chunkers.docx_structural.atoms import AtomicUnit
from rag_app.adapters.chunkers.docx_structural.context import embedding_text
from rag_app.adapters.chunkers.docx_structural.lexical import lexical_view
from rag_app.adapters.chunkers.docx_structural.packing import pack_run
from rag_app.adapters.chunkers.docx_structural.rendering import render_atoms
from rag_app.adapters.chunkers.docx_structural.reports import (
    build_chunking_report,
)
from rag_app.adapters.chunkers.docx_structural.sections import plan_sections
from rag_app.adapters.chunkers.docx_structural.validation import validate_chunks
from rag_app.adapters.tokenizers import DeterministicUtf8TokenCounter
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    Chunk,
    ChunkingContext,
    ChunkingPolicy,
    ChunkingResult,
    DocumentIR,
)
from rag_app.core.models.common import (
    JsonObject,
    freeze_json_object,
)
from rag_app.core.ports import TokenCounterPort


class DocxStructuralChunker:
    """把 P04 Document IR 转为三视图、精确来源 Chunk V3。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.CHUNKER,
        name="docx-structural-v3",
        version="3.0.0",
        mode=ProviderMode.LOCAL,
        capabilities=ComponentCapabilities(),
    )

    def __init__(
        self,
        policy: ChunkingPolicy | None = None,
        token_counter: TokenCounterPort | None = None,
    ) -> None:
        """冻结 provisional 策略和无网络 TokenCounter。

        Args:
            policy: 可选 ChunkingPolicy；缺失时使用阶段固定候选。
            token_counter: 可注入本地精确或保守估算计数器。

        Returns:
            无返回值。

        """
        self.policy = policy or ChunkingPolicy()
        self.token_counter = token_counter or DeterministicUtf8TokenCounter()
        probe = self.token_counter.count("")
        self.fingerprint = canonical_sha256(
            {
                "descriptor": self.descriptor,
                "policy": self.policy,
                "token_counter": {
                    "tokenizer_id": probe.tokenizer_id,
                    "exact": probe.exact,
                    "model_compatibility": probe.model_compatibility,
                },
            }
        )

    def chunk(
        self,
        document_ir: DocumentIR,
        context: ChunkingContext,
    ) -> ChunkingResult:
        """从既有 IR 生成 chunks，不重新打开 DOCX 或访问网络。

        Args:
            document_ir: `docx-ooxml-v4` 或兼容 Parser 的 IR。
            context: 绑定 index revision 和 chunker 指纹的上下文。

        Returns:
            有序 Chunk V3 与聚合结构报告。

        Raises:
            ValueError: 上下文指纹不匹配或任一最终不变量失败。

        """
        if context.chunker_fingerprint != self.fingerprint:
            raise ValueError(
                "ChunkingContext 与当前 chunker fingerprint 不一致。"
            )
        started = time.monotonic()
        planned = plan_sections(document_ir, self.policy)
        chunks: list[Chunk] = []
        document_title = document_ir.source.display_name
        for section in planned:
            for run in section.runs:
                packs = pack_run(
                    run,
                    document_title=document_title,
                    policy=self.policy,
                    token_counter=self.token_counter,
                )
                chunks.extend(
                    self._finalize_pack(
                        document_ir,
                        context,
                        pack,
                        document_title,
                    )
                    for pack in packs
                )
        linked = _link_neighbors(chunks)
        validate_chunks(
            linked,
            document_ir,
            self.policy,
            self.token_counter,
        )
        report = build_chunking_report(
            linked,
            document_ir,
            self.policy,
            elapsed_seconds=time.monotonic() - started,
        )
        return ChunkingResult(chunks=tuple(linked), report=report)

    def _finalize_pack(
        self,
        document_ir: DocumentIR,
        context: ChunkingContext,
        atoms: tuple[AtomicUnit, ...],
        document_title: str,
    ) -> Chunk:
        rendered = render_atoms(atoms)
        first = atoms[0]
        embedded = embedding_text(document_title, first, rendered.text)
        lexical, identifiers = lexical_view(embedded)
        citation_count = self.token_counter.count(rendered.text)
        embedding_count = self.token_counter.count(embedded)
        token_count = max(citation_count.count, embedding_count.count)
        content_sha256 = hashlib.sha256(
            rendered.text.encode("utf-8")
        ).hexdigest()
        parent_ids = {
            atom.parent_node_id
            for atom in atoms
            if atom.parent_node_id is not None
        }
        parent_node_id = (
            next(iter(parent_ids)) if len(parent_ids) == 1 else None
        )
        child_groups = tuple(
            dict.fromkeys(
                group_id for atom in atoms for group_id in atom.child_group_ids
            )
        )
        note_refs = tuple(
            dict.fromkeys(
                note_id for atom in atoms for note_id in atom.note_refs
            )
        )
        span_identity = tuple(
            span.model_dump(mode="json", exclude_none=False)
            for span in rendered.spans
        )
        generated_chunk_id = deterministic_id(
            "chunk",
            document_ir.version.document_version_id,
            self.fingerprint,
            first.role.value,
            parent_node_id,
            first.neighbor_group_id,
            span_identity,
            content_sha256,
        )
        return Chunk(
            chunk_id=generated_chunk_id,
            project_id=document_ir.document.project_id,
            knowledge_base_id=document_ir.document.knowledge_base_id,
            index_revision_id=context.index_revision_id,
            version=document_ir.version,
            chunker_fingerprint=self.fingerprint,
            role=first.role,
            parent_node_id=parent_node_id,
            section_id=first.section_id,
            neighbor_group_id=first.neighbor_group_id,
            child_group_ids=child_groups,
            note_refs=note_refs,
            source_spans=rendered.spans,
            citation_text=rendered.text,
            embedding_text=embedded,
            lexical_text=lexical,
            heading_path=first.heading_path,
            identifiers=identifiers,
            token_count=token_count,
            token_count_is_estimate=not embedding_count.exact,
            tokenizer_id=embedding_count.tokenizer_id,
            content_sha256=content_sha256,
            metadata=_pack_metadata(atoms),
        )


def _pack_metadata(atoms: tuple[AtomicUnit, ...]) -> JsonObject:
    atom_metadata: list[dict[str, JsonValue]] = [
        {
            "unit_id": atom.unit_id,
            "role": atom.role.value,
            "metadata": _json_metadata(atom.metadata),
        }
        for atom in atoms
    ]
    orphan = any(dict(atom.metadata).get("orphan") is True for atom in atoms)
    return freeze_json_object(
        {
            "atom_count": len(atoms),
            "atoms": atom_metadata,
            "orphan": orphan,
        }
    )


def _json_metadata(metadata: JsonObject) -> dict[str, JsonValue]:
    return dict(metadata)


def _link_neighbors(chunks: Sequence[Chunk]) -> list[Chunk]:
    grouped: dict[tuple[str, str], list[Chunk]] = {}
    for chunk in chunks:
        key = (
            chunk.version.document_version_id,
            chunk.neighbor_group_id,
        )
        grouped.setdefault(key, []).append(chunk)
    linked: list[Chunk] = []
    for chunk in chunks:
        group = grouped[
            (chunk.version.document_version_id, chunk.neighbor_group_id)
        ]
        index = group.index(chunk)
        linked.append(
            chunk.model_copy(
                update={
                    "previous_chunk_id": (
                        group[index - 1].chunk_id if index else None
                    ),
                    "next_chunk_id": (
                        group[index + 1].chunk_id
                        if index + 1 < len(group)
                        else None
                    ),
                }
            )
        )
    return linked
