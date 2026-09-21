"""03 实际 schema 家族共享能力合同与失效身份门禁。"""

from __future__ import annotations

import pytest

from rag_app.adapters.providers.grounded_wire import GroundedWirePayload
from rag_app.adapters.providers.structured_contract import (
    StructuredOutputCapabilityProfile,
    wb08r_structured_output_profile,
)
from rag_app.application.answering.semantic_validation import (
    SEMANTIC_VALIDATION_REVISION,
    SemanticValidationPayload,
)
from rag_app.application.retrieval.context_resolution import build_input_spans
from rag_app.application.retrieval.minimal_plan import planner_json_schema
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.core.models.query_plan import (
    GROUNDED_CLAIM_SCHEMA_REVISION,
    QUERY_PLAN_SCHEMA_REVISION,
)
from rag_app.core.models.relation_review import (
    RELATION_REVIEW_REVISION,
    RelationReviewPayload,
)


def _profile(**updates: object) -> StructuredOutputCapabilityProfile:
    values: dict[str, object] = {
        "profile_revision": "shared-structured-unit-v1",
        "service_identity_sha256": canonical_sha256("service-v1"),
        "model": "Qwen/Unit",
        "chat_template_revision": "template-v1",
        "mode": "response_format",
        "grammar_backend": "xgrammar-v1",
        "deadline_ms": 8000,
        "field_resolution_output_tokens": 512,
        "qualification_evidence_sha256": canonical_sha256("qualification-v1"),
    }
    values.update(updates)
    return wb08r_structured_output_profile(**values)  # type: ignore[arg-type]


def test_all_current_03_schema_families_are_registered_and_validated() -> None:
    profile = _profile()
    request = SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "schema-families"),
            knowledge_base_id=deterministic_id("kb", "schema-families"),
        ),
        text="甲的职责是什么？",
    )
    schemas = (
        (
            QUERY_PLAN_SCHEMA_REVISION,
            planner_json_schema(build_input_spans(request)),
        ),
        (
            GROUNDED_CLAIM_SCHEMA_REVISION,
            GroundedWirePayload.model_json_schema(),
        ),
        (
            SEMANTIC_VALIDATION_REVISION,
            SemanticValidationPayload.model_json_schema(),
        ),
        (RELATION_REVIEW_REVISION, RelationReviewPayload.model_json_schema()),
    )

    for revision, schema in schemas:
        profile.assert_schema(revision, schema)


def test_dynamic_enum_hash_is_audit_identity_not_family_allowlist() -> None:
    profile = _profile()
    first = {
        "type": "object",
        "additionalProperties": False,
        "required": ["r"],
        "properties": {
            "r": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {"type": "string", "enum": ["F1"]},
            }
        },
    }
    second = {
        **first,
        "properties": {
            "r": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {"type": "string", "enum": ["F9"]},
            }
        },
    }

    profile.assert_schema(
        "wb08r-field-resolution-v2",
        first,
        atom_count=1,
        max_candidates_per_atom=1,
        max_candidate_id_length=2,
        max_query_fragment_length=160,
    )
    profile.assert_schema(
        "wb08r-field-resolution-v2",
        second,
        atom_count=1,
        max_candidates_per_atom=1,
        max_candidate_id_length=2,
        max_query_fragment_length=160,
    )
    assert canonical_sha256(first) != canonical_sha256(second)


def test_profile_identity_invalidates_on_runtime_changes() -> None:
    baseline = _profile()
    variants = (
        _profile(service_identity_sha256=canonical_sha256("service-v2")),
        _profile(grammar_backend="xgrammar-v2"),
        _profile(chat_template_revision="template-v2"),
        _profile(
            qualification_evidence_sha256=canonical_sha256("qualification-v2")
        ),
        _profile(profile_revision="shared-structured-unit-v2"),
    )
    assert all(
        item.profile_sha256 != baseline.profile_sha256 for item in variants
    )


def test_unregistered_family_and_unsupported_keyword_fail_closed() -> None:
    profile = _profile()
    with pytest.raises(ValueError, match="FAMILY_NOT_QUALIFIED"):
        profile.assert_schema("unknown-v1", {"type": "object"})
    with pytest.raises(ValueError, match="KEYWORD_NOT_QUALIFIED"):
        profile.assert_schema(
            "wb08r-field-resolution-v2",
            {"type": "array", "uniqueItems": True},
            atom_count=1,
            max_candidates_per_atom=1,
            max_candidate_id_length=2,
            max_query_fragment_length=160,
        )
