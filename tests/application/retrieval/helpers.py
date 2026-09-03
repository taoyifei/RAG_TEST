from __future__ import annotations

import hashlib

from rag_app.core.models import (
    Chunk,
    ChunkRole,
    DocumentVersionRef,
    HydratedChunk,
    RankedChunk,
    RrfContribution,
    SourceAnchor,
    SourceSpan,
    StoryKind,
)


def make_ranked_chunk(  # noqa: PLR0913
    number: int,
    text: str,
    *,
    channel: str = "lexical:fts5",
    role: ChunkRole = ChunkRole.TEXT,
    identifiers: tuple[str, ...] = (),
    document_number: int = 2,
    section_id: str = "section-1",
    neighbor_group_id: str = "group-1",
    previous_chunk_id: str | None = None,
    next_chunk_id: str | None = None,
    must_keep: bool = False,
) -> RankedChunk:
    suffix = f"{number:032x}"
    document_suffix = f"{document_number:032x}"
    document_version_suffix = f"{document_number + 100:032x}"
    anchor = SourceAnchor(
        part_uri="/word/document.xml",
        story_kind=StoryKind.BODY,
        structural_path=("body", f"p:{number}"),
        ordinal=number,
        paragraph_index=number,
        source_start_char=0,
        source_end_char=len(text),
    )
    span = SourceSpan(
        node_id=f"node_{suffix}",
        source_anchor=anchor,
        structural_path=anchor.structural_path,
        chunk_start_char=0,
        chunk_end_char=len(text),
        source_start_char=0,
        source_end_char=len(text),
    )
    chunk = Chunk(
        chunk_id=f"chunk_{suffix}",
        project_id=f"prj_{'a' * 32}",
        knowledge_base_id=f"kb_{'b' * 32}",
        index_revision_id=f"irev_{'c' * 32}",
        version=DocumentVersionRef(
            document_id=f"doc_{document_suffix}",
            document_version_id=f"dver_{document_version_suffix}",
            content_sha256="d" * 64,
        ),
        chunker_fingerprint=f"sha256:{'e' * 64}",
        role=role,
        section_id=section_id,
        neighbor_group_id=neighbor_group_id,
        previous_chunk_id=previous_chunk_id,
        next_chunk_id=next_chunk_id,
        source_spans=(span,),
        citation_text=text,
        embedding_text=text,
        lexical_text=text.casefold(),
        identifiers=identifiers,
        token_count=max(1, len(text)),
        token_count_is_estimate=False,
        tokenizer_id="test-tokenizer",
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return RankedChunk(
        hydrated=HydratedChunk(chunk=chunk, display_name="fixture.docx"),
        fusion_rank=number,
        must_keep=must_keep,
        contributions=(
            RrfContribution(
                channel=channel,
                rank=number,
                weight=1.0,
                contribution=1.0 / (60 + number),
            ),
        ),
    )
