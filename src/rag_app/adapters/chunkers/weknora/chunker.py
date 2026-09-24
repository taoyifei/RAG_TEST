"""固定 WeKnora Go 自适应父子分块器的同步、离线适配。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rag_app.adapters.chunkers.docx_structural.lexical import lexical_view
from rag_app.adapters.chunkers.docx_structural.reports import (
    build_chunking_report,
)
from rag_app.adapters.chunkers.docx_structural.sections import plan_sections
from rag_app.adapters.chunkers.docx_structural.validation import validate_chunks
from rag_app.adapters.chunkers.weknora.reading_view import (
    ReadingView,
    build_reading_view,
    slice_source_spans,
)
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
    ChunkingReport,
    ChunkingResult,
    DocumentIR,
    ParentPassage,
    SourceSpan,
    SourceSpanKind,
    WeKnoraChunkingPolicy,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.ports import TokenCounterPort

if TYPE_CHECKING:
    from rag_app.adapters.chunkers.docx_structural.atoms import RunPlan

_UPSTREAM_COMMIT = "1edcd54b43606d9079bb36650efe3f68707a79ea"
_PINNED_BINARY_SHA256 = (
    "491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f"
)
_MAX_REQUEST_BYTES = 8 << 20
_MAX_RESPONSE_BYTES = 32 << 20
_GO_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class _GoPiece:
    content: str
    context_header: str
    seq: int
    start: int
    end: int
    parent_index: int


class WeKnoraChunkerAdapter:
    """让上游 Go 分块算法决定边界，本方只校验和持久化来源。"""

    def __init__(
        self,
        policy: WeKnoraChunkingPolicy | None = None,
        token_counter: TokenCounterPort | None = None,
        binary_path: str | Path | None = None,
    ) -> None:
        self.policy = policy or WeKnoraChunkingPolicy()
        self.token_counter = token_counter or DeterministicUtf8TokenCounter()
        self.binary_path = _resolve_binary(binary_path)
        binary_hash = hashlib.sha256(self.binary_path.read_bytes()).hexdigest()
        if binary_hash != _PINNED_BINARY_SHA256:
            raise ValueError("WeKnora chunker binary 摘要与固定构建不一致。")
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.CHUNKER,
            name="weknora-adaptive-parent-child-v1",
            version=f"1edcd54+{binary_hash[:16]}",
            source="Tencent/WeKnora",
            mode=ProviderMode.LOCAL,
            capabilities=ComponentCapabilities(),
        )
        counter_probe = self.token_counter.count("")
        self.fingerprint = canonical_sha256(
            {
                "descriptor": self.descriptor,
                "policy": self.policy,
                "binary_sha256": binary_hash,
                "tokenizer_id": counter_probe.tokenizer_id,
                "token_count_exact": counter_probe.exact,
            }
        )

    def chunk(
        self,
        document_ir: DocumentIR,
        context: ChunkingContext,
    ) -> ChunkingResult:
        """分块每个同来源结构 run，并保留父子关系和精确来源。"""
        if context.chunker_fingerprint != self.fingerprint:
            raise ValueError("WeKnora chunker fingerprint 不匹配。")
        started = time.monotonic()
        nodes = {node.node_id: node for node in document_ir.nodes}
        chunks: list[Chunk] = []
        parents: list[ParentPassage] = []
        for section in plan_sections(document_ir, self.policy):
            for run in section.runs:
                view = build_reading_view(run, nodes)
                if not view.text.strip():
                    continue
                parent_pieces, child_pieces, selected_tier = self._split(view)
                parent_ids = tuple(
                    deterministic_id(
                        "ppsg",
                        document_ir.version.document_version_id,
                        self.fingerprint,
                        run.run_id,
                        piece.seq,
                        piece.start,
                        piece.end,
                    )
                    for piece in parent_pieces
                )
                linked_children: dict[int, list[str]] = defaultdict(list)
                for piece in child_pieces:
                    if piece.parent_index >= len(parent_ids):
                        raise ValueError("Go child 指向不存在的 parent。")
                    parent_id = (
                        parent_ids[piece.parent_index]
                        if piece.parent_index >= 0
                        else None
                    )
                    spans = slice_source_spans(view, piece.start, piece.end)
                    if not any(span.is_citable for span in spans):
                        continue
                    chunk = self._make_chunk(
                        document_ir,
                        context,
                        run,
                        piece,
                        spans,
                        parent_id,
                        selected_tier,
                    )
                    chunks.append(chunk)
                    if piece.parent_index >= 0:
                        linked_children[piece.parent_index].append(
                            chunk.chunk_id
                        )
                for index, piece in enumerate(parent_pieces):
                    child_ids = tuple(linked_children[index])
                    if not child_ids:
                        continue
                    parents.append(
                        ParentPassage(
                            parent_passage_id=parent_ids[index],
                            project_id=document_ir.document.project_id,
                            knowledge_base_id=document_ir.document.knowledge_base_id,
                            index_revision_id=context.index_revision_id,
                            version=document_ir.version,
                            citation_text=piece.content,
                            source_spans=slice_source_spans(
                                view, piece.start, piece.end
                            ),
                            child_chunk_ids=child_ids,
                            content_sha256=hashlib.sha256(
                                piece.content.encode("utf-8")
                            ).hexdigest(),
                            metadata=freeze_json_object(
                                {
                                    "upstream_commit": _UPSTREAM_COMMIT,
                                    "selected_tier": selected_tier,
                                    "reading_view_revision": (
                                        self.policy.reading_view_revision
                                    ),
                                    "run_id": run.run_id,
                                    "reading_view_start_char": piece.start,
                                    "reading_view_end_char": piece.end,
                                }
                            ),
                        )
                    )
        linked = _link_neighbors(chunks)
        validate_chunks(linked, document_ir, self.policy, self.token_counter)
        report = build_chunking_report(
            linked,
            document_ir,
            self.policy,
            elapsed_seconds=time.monotonic() - started,
        )
        return ChunkingResult(
            chunks=tuple(linked),
            report=report,
            parent_passages=tuple(parents),
        )

    def validate_persisted(
        self,
        chunks: Sequence[Chunk],
        document_ir: DocumentIR,
    ) -> ChunkingReport:
        """从持久化 IR 和子块独立复算当前策略的激活报告。"""
        validate_chunks(chunks, document_ir, self.policy, self.token_counter)
        return build_chunking_report(
            chunks, document_ir, self.policy, elapsed_seconds=0.0
        )

    def validate_parent_passages(  # noqa: PLR0912
        self,
        parents: Sequence[ParentPassage],
        chunks: Sequence[Chunk],
        document_ir: DocumentIR,
    ) -> None:
        """从持久化父级、子级和 IR 验证关系及逐字来源。"""
        nodes = {node.node_id: node for node in document_ir.nodes}
        children_by_parent: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            if chunk.parent_passage_id is not None:
                children_by_parent[chunk.parent_passage_id].append(chunk)
        parent_ids = {parent.parent_passage_id for parent in parents}
        if len(parent_ids) != len(parents) or parent_ids != set(
            children_by_parent
        ):
            raise ValueError("父级记录与子级引用不一致。")
        for parent in parents:
            if parent.version != document_ir.version:
                raise ValueError("父级文档版本与 IR 不一致。")
            children = children_by_parent[parent.parent_passage_id]
            if set(parent.child_chunk_ids) != {
                child.chunk_id for child in children
            }:
                raise ValueError("父级子块成员不一致。")
            parent_meta = dict(parent.metadata)
            parent_start = parent_meta.get("reading_view_start_char")
            parent_end = parent_meta.get("reading_view_end_char")
            if not isinstance(parent_start, int) or not isinstance(
                parent_end, int
            ):
                raise ValueError("父级阅读视图区间缺失。")
            for child in children:
                child_meta = dict(child.metadata)
                start = child_meta.get("reading_view_start_char")
                end = child_meta.get("reading_view_end_char")
                if (
                    child.version != parent.version
                    or child.project_id != parent.project_id
                    or child.knowledge_base_id != parent.knowledge_base_id
                    or child.index_revision_id != parent.index_revision_id
                    or child_meta.get("run_id") != parent_meta.get("run_id")
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not parent_start <= start < end <= parent_end
                    or parent.citation_text[
                        start - parent_start : end - parent_start
                    ]
                    != child.citation_text
                ):
                    raise ValueError("子级超出同版本父级阅读材料。")
            for span in parent.source_spans:
                if span.span_type is SourceSpanKind.SEPARATOR:
                    continue
                node = nodes.get(span.node_id or "")
                if node is None or node.anchor != span.source_anchor:
                    raise ValueError("父级来源节点或锚点不一致。")
                node_metadata = dict(node.metadata)
                if node_metadata.get("citation_basis") == "parsed_artifact":
                    span_metadata = dict(span.metadata)
                    for key in (
                        "parsed_artifact_id",
                        "parsed_artifact_start_char",
                        "parsed_artifact_end_char",
                    ):
                        if span_metadata.get(key) != node_metadata.get(key):
                            raise ValueError("父级解析产物来源身份不一致。")
                observed = parent.citation_text[
                    span.chunk_start_char : span.chunk_end_char
                ]
                if span.span_type is SourceSpanKind.DERIVED_NUMBERING:
                    marker = (
                        node.list_attributes.marker
                        if node.list_attributes is not None
                        else None
                    )
                    if observed != marker:
                        raise ValueError("父级派生编号与 IR 不一致。")
                    continue
                if (
                    node.text_payload is None
                    or span.source_start_char is None
                    or span.source_end_char is None
                ):
                    raise ValueError("父级原文范围缺失。")
                expected = node.text_payload.exact_text[
                    span.source_start_char : span.source_end_char
                ]
                if span.span_type is SourceSpanKind.NORMALIZED_TEXT:
                    expected = expected.replace("\r\n", "\n").replace(
                        "\r", "\n"
                    )
                if observed != expected:
                    raise ValueError("父级来源无法从 IR 逐字恢复。")

    def _split(
        self, view: ReadingView
    ) -> tuple[tuple[_GoPiece, ...], tuple[_GoPiece, ...], str]:
        request = {
            "normalized_text": view.text,
            "mode": "parent_child",
            "strategy": self.policy.strategy,
            "chunk_size": self.policy.chunk_size_chars,
            "overlap": self.policy.overlap_chars,
            "parent_size": self.policy.parent_size_chars,
            "child_size": self.policy.child_size_chars,
            "token_limit": 0,
            "language_hints": [],
        }
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(payload) > _MAX_REQUEST_BYTES:
            raise ValueError("单次 Go 分块输入超过限制。")
        try:
            result = subprocess.run(  # noqa: S603
                [str(self.binary_path)],
                input=payload,
                capture_output=True,
                timeout=_GO_TIMEOUT_SECONDS,
                check=False,
                env={"LANG": "C.UTF-8"},
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Go 分块进程超时。") from error
        if result.returncode != 0:
            raise ValueError("Go 分块进程失败。")
        if len(result.stdout) > _MAX_RESPONSE_BYTES:
            raise ValueError("Go 分块响应超过限制。")
        try:
            response = json.loads(result.stdout)
        except (TypeError, ValueError) as error:
            raise ValueError("Go 分块响应不是有效 JSON。") from error
        if not isinstance(response, dict) or (
            response.get("upstream_commit") != _UPSTREAM_COMMIT
            or response.get("offset_unit") != "unicode_codepoint"
        ):
            raise ValueError("Go 分块版本或偏移单位不匹配。")
        parents = _parse_pieces(response.get("parents"), view.text)
        children = _parse_pieces(response.get("children"), view.text)
        diagnostics = response.get("diagnostics")
        selected_tier = (
            diagnostics.get("selected_tier")
            if isinstance(diagnostics, dict)
            else None
        )
        if not isinstance(selected_tier, str) or not selected_tier:
            raise ValueError("Go 分块未返回实际选择的策略层。")
        return parents, children, selected_tier

    def _make_chunk(  # noqa: PLR0913, PLR0917
        self,
        document_ir: DocumentIR,
        context: ChunkingContext,
        run: RunPlan,
        piece: _GoPiece,
        spans: tuple[SourceSpan, ...],
        parent_id: str | None,
        selected_tier: str,
    ) -> Chunk:
        citation = piece.content
        heading_path = run.heading_path
        embedded = (
            piece.context_header + "\n\n" + citation.strip()
            if piece.context_header
            else citation.strip()
        )
        if not embedded:
            raise ValueError("Go 生成空白子块。")
        lexical, identifiers = lexical_view(embedded)
        count = max(
            self.token_counter.count(citation).count,
            self.token_counter.count(embedded).count,
        )
        content_hash = hashlib.sha256(citation.encode("utf-8")).hexdigest()
        parent_nodes = {
            atom.parent_node_id
            for atom in run.atoms
            if atom.parent_node_id is not None
        }
        parent_node_id = (
            next(iter(parent_nodes)) if len(parent_nodes) == 1 else None
        )
        child_groups = tuple(
            dict.fromkeys(
                group_id
                for atom in run.atoms
                for group_id in atom.child_group_ids
            )
        )
        note_refs = tuple(
            dict.fromkeys(
                note_id for atom in run.atoms for note_id in atom.note_refs
            )
        )
        return Chunk(
            chunk_id=deterministic_id(
                "chunk",
                document_ir.version.document_version_id,
                self.fingerprint,
                run.run_id,
                piece.seq,
                piece.start,
                piece.end,
                content_hash,
            ),
            project_id=document_ir.document.project_id,
            knowledge_base_id=document_ir.document.knowledge_base_id,
            index_revision_id=context.index_revision_id,
            version=document_ir.version,
            chunker_fingerprint=self.fingerprint,
            role=run.role,
            parent_node_id=parent_node_id,
            parent_passage_id=parent_id,
            section_id=run.section_id,
            neighbor_group_id=run.neighbor_group_id,
            child_group_ids=child_groups,
            note_refs=note_refs,
            context_dependencies=run.context_dependencies,
            source_spans=spans,
            citation_text=citation,
            embedding_text=embedded,
            lexical_text=lexical,
            heading_path=heading_path,
            identifiers=identifiers,
            token_count=count,
            token_count_is_estimate=not self.token_counter.exact,
            tokenizer_id=self.token_counter.tokenizer_id,
            content_sha256=content_hash,
            metadata=freeze_json_object(
                {
                    "upstream_commit": _UPSTREAM_COMMIT,
                    "selected_tier": selected_tier,
                    "reading_view_revision": self.policy.reading_view_revision,
                    "run_id": run.run_id,
                    "reading_view_start_char": piece.start,
                    "reading_view_end_char": piece.end,
                }
            ),
        )


def _resolve_binary(configured: str | Path | None) -> Path:
    if configured is not None:
        resolved = Path(configured).resolve(strict=True)
    else:
        installed = shutil.which("wb-chunker")
        bundled = (
            Path(__file__).resolve().parents[5]
            / "vendor/weknora-chunker/bin/linux-amd64/wb-chunker"
        )
        resolved = (
            Path(installed).resolve()
            if installed
            else bundled.resolve(strict=True)
        )
    if not resolved.is_file():
        raise ValueError("WeKnora chunker binary 不存在。")
    return resolved


def _parse_pieces(raw: object, text: str) -> tuple[_GoPiece, ...]:
    if not isinstance(raw, list):
        raise ValueError("Go 分块列表缺失。")
    pieces: list[_GoPiece] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Go 分块结构无效。")
        content = item.get("content")
        header = item.get("context_header")
        seq = item.get("seq")
        start = item.get("start")
        end = item.get("end")
        parent_index = item.get("parent_index")
        if (
            not isinstance(content, str)
            or not isinstance(header, str)
            or any(
                type(value) is not int
                for value in (seq, start, end, parent_index)
            )
        ):
            raise ValueError("Go 分块字段无效。")
        seq = cast(int, seq)
        start = cast(int, start)
        end = cast(int, end)
        parent_index = cast(int, parent_index)
        if (
            not 0 <= start < end <= len(text)
            or parent_index < -1
            or text[start:end] != content
        ):
            raise ValueError("Go 分块偏移与正文不一致。")
        pieces.append(_GoPiece(content, header, seq, start, end, parent_index))
    if len({piece.seq for piece in pieces}) != len(pieces):
        raise ValueError("Go 分块序号重复。")
    return tuple(pieces)


def _link_neighbors(chunks: Sequence[Chunk]) -> list[Chunk]:
    grouped: dict[tuple[str, str], list[Chunk]] = defaultdict(list)
    for chunk in chunks:
        grouped[
            (chunk.version.document_version_id, chunk.neighbor_group_id)
        ].append(chunk)
    positions = {
        chunk.chunk_id: index
        for group in grouped.values()
        for index, chunk in enumerate(group)
    }
    return [
        chunk.model_copy(
            update={
                "previous_chunk_id": (
                    grouped[
                        (
                            chunk.version.document_version_id,
                            chunk.neighbor_group_id,
                        )
                    ][positions[chunk.chunk_id] - 1].chunk_id
                    if positions[chunk.chunk_id]
                    else None
                ),
                "next_chunk_id": (
                    grouped[
                        (
                            chunk.version.document_version_id,
                            chunk.neighbor_group_id,
                        )
                    ][positions[chunk.chunk_id] + 1].chunk_id
                    if positions[chunk.chunk_id] + 1
                    < len(
                        grouped[
                            (
                                chunk.version.document_version_id,
                                chunk.neighbor_group_id,
                            )
                        ]
                    )
                    else None
                ),
            }
        )
        for chunk in chunks
    ]
