import json

from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.fingerprints import (
    IndexFingerprintInput,
    ServingFingerprintInput,
    canonical_index_payload,
    compute_index_fingerprint,
    compute_serving_fingerprint,
)
from rag_app.core.models import EmbeddingSlotIdentity, EmbeddingSlotRole


def _descriptor(kind: ComponentKind, name: str) -> ComponentDescriptor:
    return ComponentDescriptor(
        kind=kind,
        name=name,
        version="1",
        mode=ProviderMode.LOCAL,
    )


def _slot(*, dimension: int = 1024) -> EmbeddingSlotIdentity:
    return EmbeddingSlotIdentity(
        slot_id="primary",
        role=EmbeddingSlotRole.PRIMARY,
        provider_id="jina-embedding",
        model="jina-embeddings-v5-text-small",
        vector_name="dense_primary",
        dimension=dimension,
        document_request_policy={"task": "retrieval.passage"},
        query_request_policy={"task": "retrieval.query"},
        normalization="l2",
    )


def _index_input(*, dimension: int = 1024) -> IndexFingerprintInput:
    return IndexFingerprintInput(
        parser=_descriptor(ComponentKind.PARSER, "legacy-docx"),
        parsing_policy={"include_headers": False, "tracked_changes": "reject"},
        ir_schema_version="1",
        enricher_versions=("normalize-v1",),
        chunker=_descriptor(ComponentKind.CHUNKER, "legacy-section-pack"),
        chunker_parameters={"hard_max": 512, "target": 384},
        token_counter_identity="tokenizer-sha256:example",  # noqa: S106
        token_count_exact=True,
        embedding_slots=(_slot(dimension=dimension),),
        lexical_schema={"tokenizer": "unicode61"},
        vector_schema={"distance": "cosine"},
        chunk_payload_schema={"version": "1"},
        absolute_path="/machine/a/input.docx",
    )


def _serving_input(*, reranker_model: str) -> ServingFingerprintInput:
    return ServingFingerprintInput(
        query_analyzer={"id": "analyzer-v1"},
        query_planner={"id": "planner-v1"},
        query_expansion_policy={"enabled": False},
        embedding_query_policies={"primary": {"task": "retrieval.query"}},
        embedding_router=_descriptor(
            ComponentKind.EMBEDDING_ROUTER,
            "embedding-router-single",
        ),
        retrieval_channels={"dense": True, "fts5": True},
        fusion={"method": "rrf", "k": 60},
        reranker=_descriptor(ComponentKind.RERANKER, "jina-reranker"),
        reranker_model=reranker_model,
        rerank_mode="provider_or_explicit_bypass",
        neighbor_parent_expansion={"enabled": True},
        evidence_policy={"max_items": 8},
        confidence_policy={"abstain": True},
        generator=_descriptor(ComponentKind.GENERATOR, "extractive"),
        generator_policy={"prompt": "v1"},
        citation_protocol={"version": "1"},
        absolute_path="C:/machine/private/input.docx",
    )


def test_field_order_does_not_change_index_fingerprint() -> None:
    first = _index_input()
    payload = first.model_dump(mode="json")
    reversed_payload = dict(reversed(tuple(payload.items())))
    second = IndexFingerprintInput.model_validate(reversed_payload)
    assert compute_index_fingerprint(first) == compute_index_fingerprint(second)


def test_index_semantics_and_embedding_dimension_change_fingerprint() -> None:
    original = _index_input()
    changed_dimension = _index_input(dimension=768)
    changed_chunker = original.model_copy(
        update={"chunker_parameters": (("hard_max", 600), ("target", 384))}
    )
    assert compute_index_fingerprint(original) != compute_index_fingerprint(
        changed_dimension
    )
    assert compute_index_fingerprint(original) != compute_index_fingerprint(
        changed_chunker
    )


def test_absolute_path_is_excluded_from_index_fingerprint() -> None:
    first = _index_input()
    second = first.model_copy(
        update={"absolute_path": "/other/host/renamed.docx"}
    )
    assert compute_index_fingerprint(first) == compute_index_fingerprint(second)


def test_reranker_only_changes_serving_fingerprint() -> None:
    index = _index_input()
    first = _serving_input(reranker_model="jina-reranker-v3.5")
    second = _serving_input(reranker_model="future-reranker")
    assert compute_index_fingerprint(index) == compute_index_fingerprint(index)
    assert compute_serving_fingerprint(first) != compute_serving_fingerprint(
        second
    )


def test_canonical_payload_is_auditable_without_path_or_body() -> None:
    rendered = canonical_index_payload(_index_input())
    decoded = json.loads(rendered)
    assert "absolute_path" not in decoded
    assert "document_text" not in rendered
    assert "/machine/a/input.docx" not in rendered
