from __future__ import annotations

import pytest

from rag_app.application.answering.validation import validate_extractive_draft
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import AnswerDraft, RetrievalPolicy
from tests.application.retrieval.helpers import make_ranked_chunk


def test_evidence_support_ids_and_extractive_draft_validate() -> None:
    evidence = EvidenceAssembler().assemble(
        (make_ranked_chunk(1, "受控原文"),), RetrievalPolicy()
    )
    draft = AnswerDraft(
        text="受控原文", cited_evidence_ids=(evidence[0].support_id,)
    )

    validate_extractive_draft(draft, evidence)


def test_extractive_validator_rejects_unsupported_claim() -> None:
    evidence = EvidenceAssembler().assemble(
        (make_ranked_chunk(1, "受控原文"),), RetrievalPolicy()
    )
    with pytest.raises(ValidationFailed):
        validate_extractive_draft(
            AnswerDraft(
                text="Evidence 外事实", cited_evidence_ids=("S1",)
            ),
            evidence,
        )


def test_extractive_validator_preserves_split_span_trailing_space() -> None:
    evidence = EvidenceAssembler().assemble(
        (make_ranked_chunk(1, "受控分段原文 "),), RetrievalPolicy()
    )
    draft = AnswerDraft(
        text="受控分段原文 ",
        cited_evidence_ids=(evidence[0].support_id,),
    )

    validate_extractive_draft(draft, evidence)
