"""实际发送包、稳定来源身份与受控预算的离线回归。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest

from rag_app.adapters.providers.aliyun_chat import (
    AliyunChatConfig,
    _natural_messages,
    message_token_estimate,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.adapters.stores.memory_retrieval_cache import (
    InMemoryRetrievalCache,
)
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.application.retrieval.generation_evidence import _identity
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import ProviderInputTooLarge, ProviderInvalidResponse
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import RequestedAnswerType, RetrievalPolicy
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import AtomStatus
from rag_app.core.models.retrieval import AnswerDraft, EvidenceItem
from rag_app.core.ports.generator import GenerationRequest
from rag_app.product.private_replay import PrivateReplayDraftRecorder
from tests.adapters.providers.test_aliyun_chat import _adapter, _response
from tests.adapters.providers.test_aliyun_chat_stream import _Chunks, _event
from tests.adapters.stores.test_memory_retrieval_cache import _result
from tests.application.answering.test_natural_grounded_answer import (
    _evidence,
    _matrix,
    _plan,
)
from tests.application.retrieval.test_evidence_table_coordinates import (
    _context,
    _table,
)


def _request(*texts: str) -> GenerationRequest:
    evidence = _evidence(*texts)
    plan = _plan("甲部门")
    return GenerationRequest(
        query=plan.standalone_query,
        evidence=evidence,
        citation_protocol="support-id-v3-quoted-natural-claims",
        query_plan=plan,
        atom_support_matrix=_matrix(
            plan, ((AtomStatus.PARTIAL, (evidence[-1].support_id,)),)
        ),
        per_atom_candidate_support_ids=(
            ("A1", tuple(item.support_id for item in evidence)),
        ),
    )


def test_source_identity_does_not_merge_different_story() -> None:
    item = _evidence("甲部门保存记录。")[0]
    span = item.source_spans[0]
    changed = item.model_copy(
        update={
            "source_spans": (
                span.model_copy(
                    update={
                        "source_anchor": span.source_anchor.model_copy(
                            update={"part_uri": "word/header1.xml"}
                        )
                    }
                ),
            )
        }
    )
    assert _identity(item) != _identity(changed)


def test_minimum_packet_budget_is_typed_input_failure() -> None:
    request = _request("甲部门保存记录。" * 500)
    with pytest.raises(ProviderInputTooLarge):
        _natural_messages(request, max_input_tokens=500)


def _intersection_request() -> GenerationRequest:
    context = _context("白桦泵的上限温度是多少")
    context = context.model_copy(
        update={
            "analysis": context.analysis.model_copy(
                update={
                    "semantics": context.analysis.semantics.model_copy(
                        update={
                            "target": "白桦泵",
                            "relation": "上限温度",
                            "answer_type": RequestedAnswerType.FACT,
                            "source": "SPAN_REFERENCED",
                        }
                    ),
                }
            ),
            "include_table_context": True,
        }
    )
    evidence = (
        EvidenceAssembler()
        .assemble_sets((_table(),), RetrievalPolicy(), context=context)
        .answer_support_set
    )
    assert {item.citation_text for item in evidence} == {
        "白桦泵",
        "上限温度",
        "82 ℃",
    }
    plan = _plan("白桦泵")
    request = _request("占位证据。")
    value = next(item for item in evidence if item.citation_text == "82 ℃")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, (value.support_id,)),))
    matrix = matrix.model_copy(
        update={
            "atoms": (
                matrix.atoms[0].model_copy(
                    update={
                        "supporting_support_keys": (stable_support_key(value),),
                    }
                ),
            ),
        }
    )
    return request.model_copy(
        update={
            "evidence": evidence,
            "query_plan": plan,
            "atom_support_matrix": matrix,
            "per_atom_candidate_support_ids": (
                ("A1", tuple(item.support_id for item in evidence)),
            ),
        }
    )


def test_table_intersection_budget_retains_real_header_row_and_value() -> None:
    request = _intersection_request()
    budget = message_token_estimate(_natural_messages(request)) - 1
    with pytest.raises(ProviderInputTooLarge):
        _natural_messages(request, max_input_tokens=budget)


def test_path_table_coordinates_project_without_scalar_coordinates() -> None:
    request = _intersection_request()
    assert all(
        item.source_spans[0].source_anchor.table_index is None
        for item in request.evidence
    )
    payload = json.loads(_natural_messages(request)[1].content)
    units = payload["read_units"]
    assert [item["text"] for item in units] == ["上限温度", "白桦泵\n82 ℃"]
    assert all(
        item["source_context"]["structure_scope"] == "literal_table_fragment"
        and item["source_context"]["table_relation_complete"] is False
        for item in units
    )
    assert {item["source_context"]["table_locator"] for item in units} == {
        "group-1"
    }
    assert all("table_cell" not in item["source_context"] for item in units)


@pytest.mark.parametrize("repair", [False, True])
def test_transport_body_has_authoritative_packet(
    tmp_path: Path, repair: bool
) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_response('{"claims":[]}'))

    request = _request("甲部门保存记录。", "甲部门审核设备。")
    if repair:
        request = request.model_copy(update={"repair_atom_ids": ("A1",)})
    adapter = _adapter(
        tmp_path, handler, config=AliyunChatConfig(egress_allowed=True)
    )
    try:
        draft = adapter.generate(request)
    finally:
        adapter.close()
    packet = getattr(draft, "prepared_packet", None)
    assert packet is not None
    sent = json.loads(payloads[0]["messages"][1]["content"])
    assert packet.sent_read_unit_ids == tuple(
        item["unit_id"] for item in sent["read_units"]
    )
    assert packet.per_atom_read_unit_ids == tuple(
        (atom["atom_id"], tuple(atom["allowed_ref_ids"]))
        for atom in sent["atoms"]
    )
    bound_keys = {
        key for _unit_id, keys in packet.read_unit_bindings for key in keys
    }
    assert set(packet.sent_support_ids) == {
        alias for alias, key in packet.alias_to_support_key if key in bound_keys
    }
    assert packet.evidence_level == "TRANSPORT_SENT"
    assert packet.messages_sha256
    assert packet.messages_sha256 == canonical_sha256(payloads[0]["messages"])
    assert packet.transport_body_sha256 == canonical_sha256(payloads[0])
    assert "prepared_packet" not in draft.model_dump(mode="json")


def test_private_capture_limit_does_not_abort_answer(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    recorder = PrivateReplayDraftRecorder(directory, capture_limit=1)
    request = _request("甲部门保存记录。")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response('{"claims":[]}'))

    adapter = _adapter(tmp_path, handler)
    try:
        draft = adapter.generate(request)
    finally:
        adapter.close()
    assert recorder.record(request, draft) == 1
    assert recorder.record(request, draft) is None
    assert len(recorder.output_path.read_text().splitlines()) == 1
    row = json.loads(recorder.output_path.read_text())
    assert row["evidence_level"] == "ADAPTER_INPUT"
    assert row["prepared_packet"]["evidence_level"] == "TRANSPORT_SENT"
    restored = GenerationRequest.model_validate(row["request"])
    assert restored.request_id == request.request_id
    assert stable_support_key(restored.evidence[0]) == stable_support_key(
        request.evidence[0]
    )


def test_stable_key_survives_alias_rank_and_relation_metadata() -> None:
    item = _evidence("甲部门保存记录。")[0]
    changed = item.model_copy(
        update={
            "evidence_id": "S99",
            "rerank_rank": 9,
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": "egrp_forged",
                    "group_complete": True,
                    "answer_support": {
                        "status": "SUPPORTED",
                        "query_target": "其他对象",
                    },
                }
            ),
        }
    )
    assert stable_support_key(changed) == stable_support_key(item)
    assert stable_support_key(
        item.model_copy(
            update={
                "document_version_id": "dver_" + "f" * 32,
            }
        )
    ) != stable_support_key(item)
    span = item.source_spans[0]
    assert stable_support_key(
        item.model_copy(
            update={
                "source_spans": (
                    span.model_copy(
                        update={
                            "source_start_char": span.source_start_char + 10,
                            "source_end_char": span.source_end_char + 10,
                        }
                    ),
                ),
            }
        )
    ) != stable_support_key(item)


def test_old_s1_protection_still_selects_source_a_after_alias_reorder() -> None:
    request = _request("甲部门保存记录。", "甲部门审核设备。")
    source_a, source_b = request.evidence
    matrix = request.atom_support_matrix
    matrix = matrix.model_copy(
        update={
            "atoms": (
                matrix.atoms[0].model_copy(
                    update={
                        "supporting_support_ids": ("S1",),
                        "supporting_support_keys": (
                            stable_support_key(source_a),
                        ),
                    }
                ),
            )
        }
    )
    request = request.model_copy(
        update={
            "evidence": (
                source_b.model_copy(update={"evidence_id": "S1"}),
                source_a.model_copy(update={"evidence_id": "S2"}),
            ),
            "atom_support_matrix": matrix,
        }
    )
    budget = message_token_estimate(_natural_messages(request)) - 1
    sent = json.loads(
        _natural_messages(request, max_input_tokens=budget)[1].content
    )
    assert [item["text"] for item in sent["read_units"]] == [
        source_a.citation_text
    ]
    assert sent["atoms"][0]["allowed_ref_ids"] == ["E1"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("repair", [False, True])
def test_compatible_natural_packet_matches_actual_schema_body(
    stream: bool,
    repair: bool,
) -> None:
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_response(
                '{"claims":[]}',
                model="synthetic-model",
                usage={
                    "prompt_tokens": 700,
                    "completion_tokens": 8,
                    "total_tokens": 708,
                },
            ),
        )

    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic-model",
            egress_allowed=True,
            structured_output_mode="response_format",
        ),
        http_client=ProviderHttpClient(
            "https://provider.example",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1,
            defer_success_observation=True,
        ),
        api_key_resolver=lambda: "",
    )
    request = _request("甲部门保存记录。")
    if repair:
        request = request.model_copy(
            update={
                "repair_atom_ids": ("A1",),
                "attempt_id": uuid4().hex,
                "repair_allowed_support_keys": (
                    ("A1", (stable_support_key(request.evidence[0]),)),
                ),
            }
        )
    try:
        draft = (
            adapter.generate_stream(
                request,
                on_claim=lambda _: None,
                cancellation=StreamCancellation(),
            )
            if stream
            else adapter.generate(request)
        )
    finally:
        adapter.close()
    packet = draft.prepared_packet
    assert packet is not None and packet.evidence_level == "TRANSPORT_SENT"
    assert len(payloads) == 1
    assert packet.transport_body_sha256 == canonical_sha256(payloads[0])
    assert packet.messages_sha256 == canonical_sha256(payloads[0]["messages"])
    assert packet.schema_tokens > 0
    assert packet.observed_prompt_tokens == 700
    assert packet.estimate_error_tokens == 700 - packet.estimated_input_tokens
    assert packet.attempt_id == request.attempt_id
    assert packet.request_id == request.request_id
    assert "甲部门" not in packet.model_dump_json()


def test_stream_packet_uses_same_message_registry(tmp_path: Path) -> None:
    captured = []
    request = GenerationRequest(
        query="合成职责",
        evidence=_evidence("甲部门保存记录。"),
        citation_protocol="support-id-v1-claims",
    )
    body = (
        _event(content='{"claims":[]}')
        + _event(finish="stop")
        + b"data: [DONE]\n\n"
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(http_request.content))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_Chunks((body,)),
        )

    adapter = _adapter(tmp_path, handler)
    try:
        draft = adapter.generate_stream(
            request, on_claim=lambda _: None, cancellation=StreamCancellation()
        )
    finally:
        adapter.close()
    assert captured[0]["stream"] is True
    assert draft.prepared_packet is not None
    assert draft.prepared_packet.transport_body_sha256 == canonical_sha256(
        captured[0]
    )
    assert draft.prepared_packet.evidence_level == "TRANSPORT_SENT"


def test_stream_transport_fallback_preserves_both_attempt_packets() -> None:
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(405, json={"error": {}})
        return httpx.Response(
            200, json=_response('{"claims":[]}', model="synthetic-model")
        )

    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic-model", egress_allowed=True
        ),
        http_client=ProviderHttpClient(
            "https://provider.example",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1,
            defer_success_observation=True,
        ),
        api_key_resolver=lambda: "",
    )
    request = GenerationRequest(
        query="设备说明",
        evidence=_evidence("设备定期维护。"),
        citation_protocol="support-id-v1-claims",
    )
    try:
        draft = adapter.generate_stream(
            request, on_claim=lambda _: None, cancellation=StreamCancellation()
        )
    finally:
        adapter.close()
    previous = getattr(draft, "previous_prepared_packets", ())
    assert len(previous) == 1
    first, final = previous[0], draft.prepared_packet
    assert first.request_id == final.request_id == request.request_id
    assert first.attempt_id == request.attempt_id
    assert final.attempt_id != first.attempt_id
    assert first.transport_body_sha256 == canonical_sha256(payloads[0])
    assert final.transport_body_sha256 == canonical_sha256(payloads[1])
    assert first.evidence_level == final.evidence_level == "TRANSPORT_SENT"
    assert len(draft.provider_calls) == 2


def test_concurrent_attempt_registries_do_not_share_s1(tmp_path: Path) -> None:
    barrier = Barrier(4)

    def handler(_: httpx.Request) -> httpx.Response:
        barrier.wait(timeout=5)
        return httpx.Response(200, json=_response('{"claims":[]}'))

    requests = tuple(
        _request(f"甲部门保存第{index}类记录。") for index in range(4)
    )
    adapter = _adapter(tmp_path, handler)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            drafts = tuple(executor.map(adapter.generate, requests))
    finally:
        adapter.close()
    packets = tuple(draft.prepared_packet for draft in drafts)
    assert len({packet.attempt_id for packet in packets}) == 4
    assert len({packet.request_id for packet in packets}) == 4
    for request, packet in zip(requests, packets, strict=True):
        assert packet.request_id == request.request_id
        assert packet.alias_to_support_key == (
            ("S1", stable_support_key(request.evidence[0])),
        )


def test_forged_complete_group_never_reaches_model_as_complete() -> None:
    request = _request("甲部门保存记录。", "甲部门审核设备。")
    request = request.model_copy(
        update={
            "evidence": tuple(
                item.model_copy(
                    update={
                        "metadata": freeze_json_object(
                            {
                                "evidence_group_id": "egrp_" + "f" * 32,
                                "group_complete": True,
                                "group_member_count": 2,
                            }
                        )
                    }
                )
                for item in request.evidence
            )
        }
    )
    payload = json.loads(_natural_messages(request)[1].content)
    assert "evidence_group" not in json.dumps(payload, ensure_ascii=False)


def test_internal_packet_fields_do_not_expand_public_serialization() -> None:
    request = _request("甲部门保存记录。")

    assert (
        "source_identity_scope"
        not in EvidenceItem.model_json_schema(mode="serialization")[
            "properties"
        ]
    )
    assert (
        "prepared_packet"
        not in AnswerDraft.model_json_schema(mode="serialization")["properties"]
    )
    assert "source_identity_scope" not in request.evidence[0].model_dump()
    assert (
        "supporting_support_keys"
        not in request.atom_support_matrix.atoms[0].model_dump()
    )
    assert "request_id" not in request.model_dump()
    assert (
        "previous_prepared_packets"
        not in AnswerDraft.model_json_schema(mode="serialization")["properties"]
    )


def test_product_cache_preserves_internal_source_identity_scope() -> None:
    """公开排除内部身份时，真实进程缓存仍须保留相同来源 key。"""
    item = _evidence("甲部门保存记录。")[0]
    assert item.source_identity_scope
    result = _result(1).model_copy(update={"evidence": (item,)})
    cache = InMemoryRetrievalCache()
    try:
        cache.put(result.cache_key, result)
        restored = cache.get(result.cache_key)
    finally:
        cache.close()
    assert restored is not None
    assert stable_support_key(restored.evidence[0]) == stable_support_key(item)
    assert "source_identity_scope" not in restored.model_dump_json()


def test_invalid_model_json_preserves_real_sent_packet(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response("{invalid"))

    adapter = _adapter(tmp_path, handler)
    request = _request("甲部门保存记录。")
    try:
        with pytest.raises(ProviderInvalidResponse) as failure:
            adapter.generate(request)
    finally:
        adapter.close()
    packet = getattr(failure.value, "_prepared_generation_packet", None)
    assert packet is not None
    assert packet.request_id == request.request_id
    assert packet.evidence_level == "TRANSPORT_SENT"
    assert packet.transport_body_sha256
