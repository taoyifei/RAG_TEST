"""复核只读原发送阅读单元内的行名和表头，不增加事实引用。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.evidence_group import EvidenceGroupKind
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import AtomStatus
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
    _pack,
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _evidence,
    _plan,
)


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "nonzero_header",
        "wrong_row",
        "wrong_column",
        "wrong_table",
        "derived_header",
        "unsent_header",
        "other_reading_unit",
        "other_subject",
        "borrow_other_column",
        "context_as_support",
    ],
)
def test_service_review_retains_only_sent_row_label_and_column_header(
    mutation: str,
) -> None:
    """首包发送整行，复核只取当前值格所需的两个上下文格。"""
    texts = (
        "乙部门" if mutation == "other_subject" else "甲部门",
        "归档",
        "应按登记顺序归档。",
        "其他事项",
    )
    by_text = {item.citation_text: item for item in _evidence(*texts)}
    source = tuple(by_text[text] for text in texts)
    evidence = tuple(
        _table_cell(item, row, column)
        for item, row, column in zip(
            source,
            (
                3 if mutation == "wrong_row" else 2,
                1 if mutation == "nonzero_header" else 0,
                2,
                2,
            ),
            (0, 3 if mutation == "wrong_column" else 4, 4, 3),
            strict=True,
        )
    )
    if mutation == "wrong_table":
        header = evidence[1]
        spans = tuple(
            span.model_copy(
                update={
                    "structural_path": tuple(
                        "tbl:2" if part == "tbl:1" else part
                        for part in span.structural_path
                    ),
                    "source_anchor": span.source_anchor.model_copy(
                        update={
                            "structural_path": tuple(
                                "tbl:2" if part == "tbl:1" else part
                                for part in span.structural_path
                            ),
                        }
                    ),
                }
            )
            for span in header.source_spans
        )
        evidence = (
            evidence[0],
            header.model_copy(update={"source_spans": spans}),
            *evidence[2:],
        )
    group = trusted_list_group(evidence).model_copy(
        update={
            "kind": EvidenceGroupKind.TABLE_ROW_GROUP,
            "metadata": freeze_json_object(
                {
                    "canonical_header_node_ids": []
                    if mutation == "derived_header"
                    else [span.node_id for span in evidence[1].source_spans],
                }
            ),
        }
    )
    evidence = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": group.group_id,
                        "evidence_group_type": group.kind.value,
                    }
                )
            }
        )
        for item in evidence
    )
    plan = _plan("甲部门要归档吗？")
    plan = plan.model_copy(
        update={
            "atoms": (
                plan.atoms[0].model_copy(
                    update={
                        "target": "甲部门",
                        "relation": "归档",
                        "original_fragment": "甲部门要归档吗？",
                    }
                ),
            )
        }
    )
    pack = replace(
        _pack(
            plan,
            evidence,
            atom_ids_by_support={
                item.support_id: (
                    ()
                    if mutation == "unsent_header" and item == evidence[1]
                    else ("A1",)
                )
                for item in evidence
            },
        ),
        trusted_source_groups=(group,),
        complete_group_ids=(group.group_id,),
        priority_source_units=(
            (
                "A1",
                tuple(
                    stable_support_key(item)
                    for item in (
                        (evidence[2],)
                        if mutation == "other_reading_unit"
                        else evidence
                    )
                ),
            ),
        ),
    )
    value = evidence[2]
    claim = _claim("C1", value.citation_text, "A1", value.support_id)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = json.loads(body["messages"][1]["content"])
        bodies.append(content)
        if len(bodies) == 1:
            payload = {"claims": [claim.model_dump(mode="json")]}
        else:
            assert len(bodies) == 2
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": "supported",
                        "supports": candidate["supports"],
                        "covered_scope": {
                            "subject": "其他事项"
                            if mutation == "borrow_other_column"
                            else "甲部门",
                            "relation": "归档",
                            "stage": "",
                            "conditions": [],
                        },
                    }
                    for candidate in content["candidates"]
                ]
            }
            if mutation == "context_as_support":
                payload["results"][0]["supports"] = [
                    {
                        "support_key": stable_support_key(evidence[0]),
                        "quote": evidence[0].citation_text,
                    }
                ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(payload, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 80,
                    "total_tokens": 380,
                },
            },
        )

    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            structured_output_mode="response_format",
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )
    outcome = _answer_with_pack(
        adapter, plan, evidence, ((AtomStatus.MISSING, ()),), pack=pack
    )
    if mutation == "unsent_header":
        assert not bodies
        assert outcome.published_claim_count == 0
        assert not outcome.prepared_packets
        return
    assert len(bodies) == 2, (
        outcome.raw_failures,
        outcome.claim_rejection_diagnostics,
    )
    valid_context = mutation in {
        "none",
        "nonzero_header",
        "borrow_other_column",
        "context_as_support",
    }
    accepted = mutation in {"none", "nonzero_header"}
    expected = evidence[:3] if valid_context else (value,)
    assert {item["support_key"] for item in bodies[1]["evidence"]} == {
        stable_support_key(item) for item in expected
    }
    assert bodies[1]["candidates"][0]["context_support_keys"] == [
        stable_support_key(item)
        for item in (evidence[:2] if valid_context else ())
    ]
    assert (
        outcome.accepted_claim_count
        == outcome.published_claim_count
        == int(accepted)
    )
    assert outcome.published_support_ids == (
        (value.support_id,) if accepted else ()
    )
    assert outcome.atom_coverage == (
        ("A1", "SUPPORTED" if accepted else "MISSING"),
    )
    assert outcome.repair_calls == 0
    assert len(outcome.prepared_packets) == 2
    assert all(
        packet.evidence_level == "TRANSPORT_SENT"
        for packet in outcome.prepared_packets
    )
    expected_status = (
        "NOT_OBSERVED" if mutation == "context_as_support" else "supported"
    )
    expected_reason = (
        "RELATION_REVIEW_RESPONSE_INVALID"
        if mutation == "context_as_support"
        else "RELATION_REVIEW_VALIDATED"
        if accepted
        else "SCOPE_NOT_IN_BOUND_SOURCE"
    )
    assert outcome.relation_review_results == (
        (
            hashlib.sha256(value.citation_text.encode()).hexdigest(),
            expected_status,
            expected_reason,
        ),
    )
