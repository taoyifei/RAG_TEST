"""同一 Chunk 的原文标识变体共享规范 Exact 键。"""

from pathlib import Path

from rag_app.core.models import Chunk, ExactSearchRequest
from tests.adapters.stores.test_fts_cjk_v2 import _document
from tests.persistence.helpers import runtime_with_kb


def test_public_ocr_identifier_punctuation_aliases_are_persisted_once(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    target = _document(
        project_id,
        knowledge_base_id,
        "公共合成验收-OCR-731",
        "Public inspection manual for device OCR-731. "
        "The measurement is shown only in the attached image. "
        "OCR-731 temperature: 27 Celsius",
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(target,),
            idempotency_key="public-ocr-identifier-aliases",
            budgets=runtime.default_budgets(),
        )
        with runtime.connections.transaction() as connection:
            aliases = connection.execute(
                "SELECT identifier, normalized_identifier "
                "FROM exact_identifiers WHERE revision_id=? "
                "AND normalized_identifier='ocr-731'",
                (result.revision_id,),
            ).fetchall()
            stored = connection.execute(
                "SELECT chunk_json FROM chunks WHERE revision_id=?",
                (result.revision_id,),
            ).fetchone()
        assert len(aliases) == 1
        assert aliases[0]["identifier"] == "OCR-731."
        assert stored is not None
        chunk = Chunk.model_validate_json(stored["chunk_json"])
        assert "OCR-731." in chunk.identifiers
        assert "OCR-731" in chunk.identifiers
        revision = runtime.control.revision_vector_spec(
            result.revision_id
        ).revision
        for identifier in ("OCR-731.", "OCR-731", "ocr_731", "ＯＣＲ－７３１"):
            hits = runtime.components.lexical_store.search_exact_candidates(
                ExactSearchRequest(revision=revision, identifiers=(identifier,))
            )
            assert len(hits) == 1
            assert hits[0].chunk_id == chunk.chunk_id
    finally:
        runtime.close()
