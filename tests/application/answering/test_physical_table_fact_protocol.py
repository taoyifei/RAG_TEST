"""物理表格事实从证据包到发布前校验使用同一个事实身份。"""

from __future__ import annotations

import json

import httpx

from rag_app.adapters.providers.aliyun_chat import (
    _natural_messages,
    _prepare_natural_messages,
    message_token_estimate,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.application.answering.grounded import GroundedAnsweringService
from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.generation_evidence import (
    GenerationEvidencePack,
)
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
)
from rag_app.core.ports import GenerationRequest
from tests.application.retrieval.test_generation_evidence_pack import (
    _pack,
    _plan,
    _request,
)
from tests.application.retrieval.test_generation_reading_units import (
    _sources,
    _table_cell,
)


def _fact_plan(question: str = "工装试制的输入是什么？") -> QueryPlan:
    atom = QueryAtom(
        atom_id="A1",
        target="工装试制",
        relation="输入",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    return _plan(atom).model_copy(
        update={
            "standalone_query": question,
            "original_query": question,
            "resolved_root_query": question,
        }
    )


def _generation_request(
    plan: QueryPlan, pack: GenerationEvidencePack
) -> GenerationRequest:
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.MISSING),)
    )
    return GenerationRequest(
        query=plan.standalone_query,
        evidence=pack.evidence,
        citation_protocol="support-id-v4-table-facts",
        query_plan=plan,
        atom_support_matrix=matrix,
        per_atom_candidate_support_ids=pack.per_atom_candidate_support_ids,
        priority_source_units=pack.priority_source_units,
        physical_table_facts=pack.physical_table_facts,
        atom_fact_bindings=pack.atom_fact_bindings,
    )


def test_fact_identity_is_query_and_chunk_split_invariant() -> None:
    plan = _fact_plan()
    baseline = _pack(plan, _sources())
    paraphrased = _pack(_fact_plan("工装试制要准备哪些输入？"), _sources())
    first = _table_cell(132, "图纸基线、", 2, 2)
    second = _table_cell(133, "材料清单。", 2, 2)
    sources = _sources()
    split = _pack(plan, (*sources[:2], first, second, *sources[3:]))

    assert len(baseline.physical_table_facts) == 1
    assert len(paraphrased.physical_table_facts) == 1
    assert len(split.physical_table_facts) == 1
    fact_id = baseline.physical_table_facts[0].fact_id
    assert paraphrased.physical_table_facts[0].fact_id == fact_id
    assert split.physical_table_facts[0].fact_id == fact_id
    assert len(split.physical_table_facts[0].value_support_ids) == 2


def test_multilevel_nonzero_canonical_headers_close_physical_fact() -> None:
    label = _table_cell(210, "工装试制", 3, 0).model_copy(
        update={"rerank_rank": 1}
    )
    value = _table_cell(211, "图纸基线、材料清单。", 3, 2)
    top_header = _table_cell(213, "交付资料", 0, 1)
    header = _table_cell(212, "输入", 1, 2)
    top_chunk = top_header.hydrated.chunk
    top_metadata = dict(top_chunk.metadata)
    top_atoms = top_metadata["atoms"]
    assert isinstance(top_atoms, list) and isinstance(top_atoms[0], dict)
    top_atom_metadata = top_atoms[0]["metadata"]
    assert isinstance(top_atom_metadata, dict)
    top_atom_metadata["header_strategy"] = "tblHeader"
    top_atom_metadata["cell_coordinates"] = ["r0:c1:rs1:cs2"]
    top_header = top_header.model_copy(
        update={
            "hydrated": top_header.hydrated.model_copy(
                update={
                    "chunk": top_chunk.model_copy(
                        update={"metadata": freeze_json_object(top_metadata)}
                    )
                }
            )
        }
    )
    chunk = header.hydrated.chunk
    metadata = dict(chunk.metadata)
    atoms = metadata["atoms"]
    assert isinstance(atoms, list) and isinstance(atoms[0], dict)
    atom_metadata = atoms[0]["metadata"]
    assert isinstance(atom_metadata, dict)
    atom_metadata["header_strategy"] = "tblHeader"
    header = header.model_copy(
        update={
            "hydrated": header.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"metadata": freeze_json_object(metadata)}
                    )
                }
            )
        }
    )

    pack = _pack(_fact_plan(), (label, value, top_header, header))

    assert len(pack.physical_table_facts) == 1
    fact = pack.physical_table_facts[0]
    assert {item.row_index for item in fact.headers} == {0, 1}
    assert any(item.column_indexes == (1, 2) for item in fact.headers)
    assert fact.value_column_index == 2


def test_model_sees_one_table_read_unit_without_internal_fact_id() -> None:
    plan = _fact_plan()
    pack = _pack(plan, _sources())
    request = _generation_request(plan, pack)
    fact = pack.physical_table_facts[0]
    payload = json.loads(_natural_messages(request)[1].content)

    table_units = [
        unit for unit in payload["read_units"] if unit["kind"] == "table_fact"
    ]
    assert len(table_units) == 1
    assert "图纸基线、材料清单" in table_units[0]["text"]
    assert "fact_id" not in table_units[0]
    assert payload["atoms"][0]["allowed_ref_ids"] == [table_units[0]["unit_id"]]
    assert fact.fact_id not in json.dumps(payload, ensure_ascii=False)


def test_multi_atom_fact_catalog_sends_each_source_and_fact_once() -> None:
    """多 Atom 可共享物理事实目录，但不得重复展开来源或角色。"""
    atoms = (
        QueryAtom(
            atom_id="A1",
            target="工装试制",
            relation="准备事项",
            answer_shape=AtomAnswerShape.ENUMERATION,
        ),
        QueryAtom(
            atom_id="A2",
            target="工装试制",
            relation="启动要求",
            answer_shape=AtomAnswerShape.FACT,
        ),
    )
    question = "工装试制要准备什么，满足什么要求后可以启动？"
    plan = _plan(*atoms).model_copy(
        update={
            "standalone_query": question,
            "original_query": question,
            "resolved_root_query": question,
        }
    )
    pack = _pack(plan, _sources())
    request = GenerationRequest(
        query=question,
        evidence=pack.evidence,
        citation_protocol="support-id-v4-table-facts",
        query_plan=plan,
        atom_support_matrix=AtomSupportMatrix(
            atoms=tuple(
                AtomSupport(atom_id=atom.atom_id, status=AtomStatus.MISSING)
                for atom in atoms
            )
        ),
        per_atom_candidate_support_ids=pack.per_atom_candidate_support_ids,
        priority_source_units=pack.priority_source_units,
        physical_table_facts=pack.physical_table_facts,
        atom_fact_bindings=pack.atom_fact_bindings,
    )

    prepared = _prepare_natural_messages(
        request,
        max_input_tokens=4_159,
        retain_budget_rejection=True,
    )
    payload = json.loads(prepared.messages[1].content)
    facts = [
        unit for unit in payload["read_units"] if unit["kind"] == "table_fact"
    ]
    bindings = {
        item["atom_id"]: item["allowed_ref_ids"] for item in payload["atoms"]
    }

    assert not prepared.input_budget_exceeded
    assert message_token_estimate(prepared.messages) <= 4_159
    assert len(facts) == len(pack.physical_table_facts) == 4
    assert len({fact["unit_id"] for fact in facts}) == len(facts)
    assert set(bindings) == {"A1", "A2"}
    assert all(len(unit_ids) == 4 for unit_ids in bindings.values())
    assert all("fact_id" not in item for item in facts)
    assert len(prepared.retained_table_fact_ids) == 4


def test_value_and_header_without_physical_row_label_cannot_form_fact() -> None:
    value = _table_cell(310, "图纸基线、材料清单。", 3, 2)
    header = _table_cell(311, "输入", 0, 2)

    pack = _pack(_fact_plan(), (value, header))

    assert pack.physical_table_facts == ()
    assert pack.atom_fact_bindings == ()


def test_full_answer_service_publishes_selected_physical_fact() -> None:
    plan = _fact_plan()
    pack = _pack(plan, _sources())
    fact = pack.physical_table_facts[0]
    sent_bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        sent = json.loads(body["messages"][1]["content"])
        sent_bodies.append(sent)
        if len(sent_bodies) == 1:
            table_unit = next(
                unit
                for unit in sent["read_units"]
                if unit["kind"] == "table_fact"
                and "图纸基线、材料清单" in unit["text"]
            )
            payload = {
                "claims": [
                    {
                        "atom_id": "A1",
                        "text": "工装试制的输入包括图纸基线、材料清单。",
                        "refs": [table_unit["unit_id"]],
                    }
                ]
            }
        else:
            payload = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": "supported",
                    }
                    for candidate in sent["candidates"]
                ]
            }
        return httpx.Response(
            200,
            json={
                "model": "synthetic",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(payload, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            disable_thinking_supported=True,
            disable_thinking=True,
            structured_output_mode="response_format",
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handle)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.MISSING),)
    )

    outcome = GroundedAnsweringService(adapter).answer(
        plan.standalone_query,
        pack.evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        analysis=QueryAnalyzer().analyze(_request(plan.standalone_query)),
        query_plan=plan,
        atom_support_matrix=matrix,
        generation_evidence_pack=pack,
    )

    assert outcome.answer is not None
    assert "图纸基线、材料清单" in outcome.answer
    assert outcome.accepted_claim_count == outcome.published_claim_count == 1
    assert set(outcome.accepted_support_ids) == set(fact.all_support_ids)
    assert outcome.reason_code == "DETERMINISTIC_ANSWER_PLAN"
    assert outcome.relation_review_calls == 0
    assert outcome.relation_review_skip_reason == "DETERMINISTIC_EXECUTION"
    assert outcome.repair_calls == 0
    assert not outcome.calls
    assert not outcome.prepared_packets
    assert not sent_bodies


def test_deterministic_fact_does_not_depend_on_model_response_contract() -> (
    None
):
    """源结构事实不发送模型请求，因此模型坏响应不能影响发布。"""
    plan = _fact_plan()
    pack = _pack(plan, _sources())
    sent_bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent = json.loads(body["messages"][1]["content"])
        sent_bodies.append(sent)
        return httpx.Response(
            200,
            json={
                "model": "synthetic",
                "choices": [
                    {
                        "message": {"content": "not-json"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model="synthetic",
            egress_allowed=True,
            disable_thinking_supported=True,
            disable_thinking=True,
            structured_output_mode="response_format",
        ),
        http_client=ProviderHttpClient(
            "https://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handle)),
            max_attempts=1,
        ),
        api_key_resolver=lambda: "",
    )
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.MISSING),)
    )

    outcome = GroundedAnsweringService(adapter).answer(
        plan.standalone_query,
        pack.evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        analysis=QueryAnalyzer().analyze(_request(plan.standalone_query)),
        query_plan=plan,
        atom_support_matrix=matrix,
        generation_evidence_pack=pack,
    )

    assert outcome.answer is not None
    assert "图纸基线、材料清单" in outcome.answer
    assert outcome.repair_calls == 0
    assert outcome.accepted_claim_count == outcome.published_claim_count == 1
    assert outcome.reason_code == "DETERMINISTIC_ANSWER_PLAN"
    assert not outcome.calls
    assert not outcome.prepared_packets
    assert not sent_bodies
