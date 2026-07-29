import hashlib

from rag_app.active_evidence import (
    ActiveEvidenceManifest,
    ActiveEvidenceRecord,
)
from rag_app.contracts import ChunkRole, ChunkSourceSpan, Locator


def active_evidence_record(
    *,
    chunk_id: str,
    source_path: str,
    locator: str,
    text: str,
) -> ActiveEvidenceRecord:
    source_digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    source_locator = Locator(
        file_path=source_path,
        paragraph_index=1,
        segment_index=1,
        fragment=text,
    )
    return ActiveEvidenceRecord(
        chunk_id=chunk_id,
        source_id=f"src_{source_digest[:32]}",
        source_path=source_path,
        doc_version=f"sha256:{source_digest}",
        section_id="section_" + "a" * 32,
        neighbor_group_id="group_" + "b" * 32,
        chunk_role=ChunkRole.TEXT,
        locator=locator,
        locators=(source_locator,),
        source_spans=(
            ChunkSourceSpan(
                element_id=f"element-{chunk_id}",
                locator=source_locator,
                start_char=0,
                end_char=len(text),
                source_start_char=0,
                source_end_char=len(text),
            ),
        ),
        text=text,
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def active_evidence_manifest(
    records: tuple[ActiveEvidenceRecord, ...],
) -> ActiveEvidenceManifest:
    return ActiveEvidenceManifest.create(
        collection_name="test-active-collection",
        index_manifest_sha256="a" * 64,
        pipeline_fingerprint="sha256:" + ("b" * 64),
        records=records,
    )
