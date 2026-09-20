"""复核只读原发送阅读单元内的行名和表头，不增加事实引用。"""

from __future__ import annotations

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
    ],
)
def test_service_binds_only_closed_table_read_units(
    mutation: str,
) -> None:
    """不闭合的表格坐标不能降级成可独立引用的普通段落。"""
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
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = json.loads(body["messages"][1]["content"])
        bodies.append(content)
        if len(bodies) == 1:
            table_units = [
                unit
                for unit in content["read_units"]
                if unit["kind"] == "table_fact"
            ]
            payload = {
                "claims": [
                    {
                        "atom_id": "A1",
                        "text": value.citation_text,
                        "refs": [table_units[0]["unit_id"]],
                    }
                ]
                if table_units
                else []
            }
        else:
            assert len(bodies) == 2
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": "contradicted"
                        if mutation == "other_subject"
                        else "supported",
                    }
                    for candidate in content["candidates"]
                ]
            }
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
    assert pack.physical_table_facts == ()
    assert len(bodies) == 1
    assert bodies[0]["read_units"] == []
    assert outcome.accepted_claim_count == outcome.published_claim_count == 0
    assert outcome.published_support_ids == ()
    assert outcome.atom_coverage == (("A1", "MISSING"),)
    assert outcome.repair_calls == 0
    assert len(outcome.prepared_packets) == 1
    assert all(
        packet.evidence_level == "TRANSPORT_SENT"
        for packet in outcome.prepared_packets
    )
    assert outcome.relation_review_results == ()
