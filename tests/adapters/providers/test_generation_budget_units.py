"""真实裁剪断点对应的合成最小阅读单元与预算拒绝回归。"""

from __future__ import annotations

import json

import httpx
import pytest

from rag_app.adapters.providers.aliyun_chat import (
    _natural_messages,
    message_token_estimate,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.core.errors import ProviderInputTooLarge
from rag_app.core.models import EvidenceGroupKind, SourceSpanKind
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import AtomStatus
from rag_app.core.models.retrieval import EvidenceItem
from rag_app.core.ports.generator import GenerationRequest
from tests.adapters.providers.generation_packet_helpers import trusted_groups
from tests.application.answering.test_natural_grounded_answer import (
    _evidence,
    _matrix,
    _plan,
)


def _request(evidence: tuple[EvidenceItem, ...]) -> GenerationRequest:
    plan = _plan("甲组负责的事项")
    return GenerationRequest(
        query=plan.standalone_query,
        evidence=evidence,
        query_plan=plan,
        atom_support_matrix=_matrix(plan, ((AtomStatus.MISSING, ()),)),
        citation_protocol="support-id-v3-quoted-natural-claims",
        per_atom_candidate_support_ids=(
            ("A1", tuple(item.support_id for item in evidence)),
        ),
    )


def _continuous_sources() -> tuple[EvidenceItem, ...]:
    items = _evidence(
        "甲组负责交付；", "独立编号。", "并归档结果。", "乙组负责巡检。"
    )
    first, numbering, second, other = items
    first_span = first.source_spans[0]
    assert first_span.source_anchor is not None
    start = first_span.source_start_char
    assert isinstance(start, int)
    second_span = second.source_spans[0].model_copy(
        update={
            "node_id": first_span.node_id,
            "source_anchor": first_span.source_anchor,
            "structural_path": first_span.structural_path,
            "source_start_char": start + len(first.citation_text),
            "source_end_char": start
            + len(first.citation_text)
            + len(second.citation_text),
        }
    )
    numbering_span = numbering.source_spans[0].model_copy(
        update={
            "node_id": first_span.node_id,
            "source_anchor": first_span.source_anchor,
            "structural_path": first_span.structural_path,
            "span_type": SourceSpanKind.DERIVED_NUMBERING,
            "source_start_char": None,
            "source_end_char": None,
        }
    )
    return (
        first,
        numbering.model_copy(update={"source_spans": (numbering_span,)}),
        second.model_copy(update={"source_spans": (second_span,)}),
        other,
    )


def test_missing_offsets_cannot_break_a_protected_continuous_source() -> None:
    """不可定位的派生项不应使同节点的完整原句后半段被裁掉。"""
    evidence = _continuous_sources()
    request = _request(evidence)
    supported = request.atom_support_matrix.atoms[0].model_copy(
        update={"supporting_support_keys": (stable_support_key(evidence[0]),)}
    )
    request = request.model_copy(
        update={
            "atom_support_matrix": request.atom_support_matrix.model_copy(
                update={"atoms": (supported,)}
            )
        }
    )
    pair = (evidence[0], evidence[2])
    budget = message_token_estimate(_natural_messages(_request(pair)))

    payload = json.loads(
        _natural_messages(request, max_input_tokens=budget)[1].content
    )

    assert {item["support_id"] for item in payload["evidence"]} == {
        item.support_id for item in pair
    }
    assert [item["text"] for item in payload["evidence"]] == [
        item.citation_text for item in pair
    ]


def test_application_reading_unit_survives_missing_semantic_certificate() -> (
    None
):
    """来源阅读许可不等同于语义支持；MISSING 不得抹掉选定阅读单元。"""
    evidence = _continuous_sources()
    pair = (evidence[0], evidence[2])
    original = _request(evidence)
    request = GenerationRequest(
        **{
            name: getattr(original, name)
            for name in type(original).model_fields
            if name != "priority_source_units"
        },
        priority_source_units=(
            ("ROOT", tuple(stable_support_key(item) for item in pair)),
            ("A1", tuple(stable_support_key(item) for item in pair)),
        ),
    )
    budget = message_token_estimate(_natural_messages(_request(pair)))

    payload = json.loads(
        _natural_messages(request, max_input_tokens=budget)[1].content
    )

    assert {item["support_id"] for item in payload["evidence"]} == {
        item.support_id for item in pair
    }


def _small_table_request() -> GenerationRequest:
    evidence = _evidence(
        "编号",
        "名称",
        "责任主体",
        "登记说明",
        "乙类",
        "合成设备登记材料",
        "甲组",
        "未经审核不得提交",
    )
    group_id = "egrp_" + "a" * 32
    sources = []
    for index, item in enumerate(evidence):
        span = item.source_spans[0]
        assert span.source_anchor is not None
        path = (
            "body",
            "tbl:7",
            f"tr:{0 if index < 4 else 3}",
            f"tc:{index % 4}",
            "p:1",
        )
        anchor = span.source_anchor.model_copy(update={"structural_path": path})
        sources.append(
            item.model_copy(
                update={
                    "source_spans": (
                        span.model_copy(
                            update={
                                "source_anchor": anchor,
                                "structural_path": path,
                            }
                        ),
                    ),
                    "table_locator": "group_" + "b" * 32,
                    "heading_path": (
                        "资产登记和移交管理要求",
                        "执行阶段责任及条件说明",
                        "合成完整来源行的材料登记说明",
                    ),
                    "metadata": freeze_json_object(
                        {
                            "document_title": "合成设备材料登记维护规范",
                            "evidence_group_id": group_id,
                            "evidence_group_type": "TABLE_ROW_GROUP",
                            "group_member_count": 2,
                            "group_member_index": 1 if index < 4 else 2,
                        }
                    ),
                }
            )
        )
    items = tuple(sources)
    groups = tuple(
        group.model_copy(update={"kind": EvidenceGroupKind.TABLE_ROW_GROUP})
        for group in trusted_groups(items)
    )
    return _request(items).model_copy(update={"trusted_source_groups": groups})


def test_small_complete_table_fits_without_losing_header_or_condition() -> None:
    """删除重复内部标识的渲染，同时保留同表真实行列和否定条件。"""
    request = _small_table_request()
    payload = json.loads(
        _natural_messages(request, max_input_tokens=4882)[1].content
    )

    assert [item["text"] for item in payload["evidence"]] == [
        item.citation_text for item in request.evidence
    ]
    cells = [
        item["source_structure"]["table_cell"] for item in payload["evidence"]
    ]
    assert len({cell["table_key"] for cell in cells}) == 1
    assert {cell["row"] for cell in cells} == {0, 3}
    assert {cell["column"] for cell in cells} == {0, 1, 2, 3}


def test_budget_rejection_keeps_actual_preparation_without_transport() -> None:
    """真正超预算的原文保留准备快照，不声称发生模型调用。"""
    item = _evidence("甲组不得在审核前交付。")[0]
    text = item.citation_text * 800
    span = item.source_spans[0]
    item = item.model_copy(
        update={
            "citation_text": text,
            "source_spans": (
                span.model_copy(
                    update={
                        "chunk_end_char": len(text),
                        "source_end_char": span.source_start_char + len(text),
                    }
                ),
            ),
        }
    )
    request = _request((item,))
    calls = []

    def unexpected_send(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        raise AssertionError("输入预算失败不应调用HTTP。")

    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            max_input_tokens=6144,
            max_output_tokens=1536,
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(unexpected_send)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )
    with pytest.raises(ProviderInputTooLarge) as captured:
        adapter.generate(request)

    packet = vars(captured.value).get("_prepared_generation_packet")
    assert packet is not None
    assert packet.evidence_level == "PREPARATION_REJECTED"
    assert packet.preparation_failure == "GENERATION_INPUT_BUDGET_EXCEEDED"
    assert (
        packet.estimated_input_tokens + packet.safety_margin_tokens
        > packet.max_input_tokens
    )
    assert packet.transport_body_sha256 is None
    assert packet.observed_prompt_tokens is None
    assert calls == []


def test_priority_intersection_keeps_headers_and_drops_other_cells() -> None:
    """选定的交点阅读单元可独立保留，其他列移除后不声称整组完整。"""
    original = _small_table_request()
    members = tuple(original.evidence[index] for index in (1, 4, 5))
    units = (("A1", tuple(stable_support_key(item) for item in members)),)
    request = original.model_copy(update={"priority_source_units": units})
    minimum = _request(members).model_copy(
        update={
            "trusted_source_groups": original.trusted_source_groups,
            "priority_source_units": units,
        }
    )
    budget = message_token_estimate(_natural_messages(minimum))

    payload = json.loads(
        _natural_messages(request, max_input_tokens=budget)[1].content
    )

    assert {item["support_id"] for item in payload["evidence"]} == {
        item.support_id for item in members
    }
    assert all(
        item["evidence_group"]["complete"] is False
        for item in payload["evidence"]
    )
    assert {
        item["source_structure"]["table_cell"]["row"]
        for item in payload["evidence"]
    } == {0, 3}


def test_repair_does_not_reintroduce_root_or_other_atom_reading_unit() -> None:
    """局部修复只保护失败 Atom 的当前许可，不重新加入 Root 或已过 Atom。"""
    evidence = _evidence("甲组负责交付。", "乙组负责巡检。")
    plan = _plan("甲组", "乙组")
    request = GenerationRequest(
        query=plan.standalone_query,
        evidence=evidence,
        query_plan=plan,
        atom_support_matrix=_matrix(plan, ((AtomStatus.MISSING, ()),) * 2),
        citation_protocol="support-id-v3-quoted-natural-claims",
        per_atom_candidate_support_ids=(("A1", ("S1",)), ("A2", ("S2",))),
        priority_source_units=(
            ("ROOT", (stable_support_key(evidence[0]),)),
            ("A1", (stable_support_key(evidence[0]),)),
            ("A2", (stable_support_key(evidence[1]),)),
        ),
        repair_atom_ids=("A2",),
        repair_allowed_support_keys=(
            ("A2", (stable_support_key(evidence[1]),)),
        ),
    )

    payload = json.loads(_natural_messages(request)[1].content)

    assert [item["support_id"] for item in payload["evidence"]] == ["S2"]
    assert [atom["atom_id"] for atom in payload["atoms"]] == ["A2"]
    assert payload["repair_only"] is True


@pytest.mark.parametrize("owner", ["UNKNOWN", "A1"])
def test_priority_unit_cannot_name_unknown_owner_or_source(owner: str) -> None:
    """内部阅读单元仍必须绑定合法 Atom 与真实来源，而非任意字符串。"""
    original = _request(_evidence("甲组负责交付。"))
    with pytest.raises(ValueError, match="优先阅读单元"):
        GenerationRequest(
            **{
                name: getattr(original, name)
                for name in type(original).model_fields
                if name != "priority_source_units"
            },
            priority_source_units=((owner, ("sha256:unknown",)),),
        )
