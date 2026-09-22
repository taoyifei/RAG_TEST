"""复核输入只包含当前引用与必要语境，原全文不重复占用预算。"""

from __future__ import annotations

import json

import httpx
import pytest

from rag_app.core.errors import ProviderInvalidResponse
from rag_app.core.models.generation_packet import stable_support_key
from tests.adapters.providers.test_relation_review import (
    _adapter,
    _payload,
    _request,
)


def test_review_omits_unquoted_text_and_repeated_root_analysis() -> None:
    request = _request()
    signals = {
        "quoted_phrases": ("甲手册",),
        "identifiers": ("MX-17",),
        "numbers": ("14",),
        "units": ("天",),
        "date_version_signals": ("2026年",),
        "negation_signals": ("不得",),
        "structural_table_signals": ("表格",),
    }
    request = request.model_copy(
        update={
            "candidates": tuple(
                candidate.model_copy(
                    update={
                        "analysis": candidate.analysis.model_copy(
                            update={
                                **signals,
                                "resolved_query": "甲手册中甲部门的归档限制",
                            }
                        )
                    }
                )
                for candidate in request.candidates
            )
        }
    )
    original = request.evidence[0]
    full_source = "附加背景" * 3000 + original.citation_text + "其余说明" * 3000
    span = original.source_spans[0]
    assert span.source_anchor is not None
    source = original.model_copy(
        update={
            "citation_text": full_source,
            "source_spans": (
                span.model_copy(
                    update={
                        "source_start_char": 0,
                        "source_end_char": len(full_source),
                        "chunk_start_char": 0,
                        "chunk_end_char": len(full_source),
                        "source_anchor": span.source_anchor.model_copy(
                            update={
                                "source_start_char": 0,
                                "source_end_char": len(full_source),
                            }
                        ),
                    }
                ),
            ),
        }
    )
    evidence = (source, request.evidence[1])
    keys = tuple(
        (item.support_id, stable_support_key(item)) for item in evidence
    )
    packet = request.sent_packet.model_copy(
        update={
            "alias_to_support_key": keys,
            "original_support_keys": tuple(key for _, key in keys),
        }
    )
    request = type(request).model_validate(
        {
            **request.model_dump(),
            "evidence": evidence,
            "sent_packet": packet,
        }
    )
    payload = _payload(request)
    sent: list[httpx.Request] = []
    result = _adapter(payload, sent).review_relations(request)
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    data = json.loads(body["messages"][1]["content"])
    assert data["original_query"] == request.original_query
    assert "附加背景" not in json.dumps(data, ensure_ascii=False)
    assert "其余说明" not in json.dumps(data, ensure_ascii=False)
    assert data["evidence"][0]["quote_anchors"] == [
        {"anchor_id": "E1Q1", "quote": original.citation_text}
    ]
    assert all("analysis" not in candidate for candidate in data["candidates"])
    assert all(
        "semantics" in candidate
        and "question" in candidate
        and "atom_constraints" in candidate
        for candidate in data["candidates"]
    )
    for candidate in data["candidates"]:
        assert "trusted_signals" not in candidate
        assert candidate["question"] == "甲手册中甲部门的归档限制"
    assert result.prepared_packet.estimated_input_tokens <= 6144
    assert result.prepared_packet.reserved_output_tokens == 1024
    assert all(
        item["sent_quote_sha256s"]
        for item in result.prepared_packet.support_sources
    )

    invalid = _payload(request)
    invalid["results"][0]["source_scope"]["relation_anchor_ids"] = [
        "E1Q999"
    ]
    with pytest.raises(ProviderInvalidResponse):
        _adapter(invalid, []).review_relations(request)


@pytest.mark.parametrize("context", [("S2",), ("S99",), ("S1", "S1")])
def test_request_cannot_borrow_another_claim_or_unsent_context(
    context: tuple[str, ...],
) -> None:
    request = _request()
    candidate = request.candidates[0].model_copy(
        update={"context_support_ids": context}
    )
    with pytest.raises(ValueError, match="RELATION_REVIEW_CONTEXT_NOT_PROVED"):
        type(request).model_validate(
            {
                **request.model_dump(),
                "candidates": (candidate, request.candidates[1]),
                "evidence": request.evidence,
                "sent_packet": request.sent_packet,
            }
        )
