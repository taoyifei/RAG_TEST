"""生成证据准入只处理安全边界，语义证明留给生成后 Claim。"""

from __future__ import annotations

from dataclasses import replace
from typing import TypedDict, Unpack

from rag_app.application.retrieval.evidence import _evidence_item
from rag_app.application.retrieval.evidence_groups import (
    GroupCandidate,
    build_evidence_groups,
)
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionReason,
    EvidenceAdmissionStatus,
    GenerationEvidencePack,
    build_generation_evidence_pack,
)
from rag_app.core.models import (
    ChunkRole,
    EvidenceGroup,
    EvidenceGroupKind,
    EvidenceItem,
    GroupSourceMap,
    KnowledgeBaseScope,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
    SourceSpan,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    AtomConstraintKind,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryConstraint,
    QueryPlan,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _plan(*atoms: QueryAtom) -> QueryPlan:
    return QueryPlan(
        plan_id=f"sha256:{'a' * 64}",
        standalone_query="设备检修时限是多少？",
        original_query="设备检修时限是多少？",
        resolved_root_query="设备检修时限是多少？",
        context_digest=f"sha256:{'b' * 64}",
        intent="fact",
        effort="DIRECT",
        atoms=atoms,
        planner_reason_code="TEST",
    )


def _request(text: str = "设备检修时限是多少？") -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'a' * 32}",
            knowledge_base_id=f"kb_{'b' * 32}",
        ),
        text=text,
    )


def _item(number: int, text: str) -> tuple[RankedChunk, EvidenceItem]:
    candidate = make_ranked_chunk(number, text)
    span = candidate.hydrated.chunk.source_spans[0]
    evidence = _evidence_item(candidate, span, text, "S1")
    return candidate, evidence


class _PackOptions(TypedDict, total=False):
    root: tuple[EvidenceItem, ...]
    atom: tuple[EvidenceItem, ...]
    links: tuple[AtomCandidateLink, ...]
    groups: tuple[GroupCandidate, ...]
    request: SearchRequest | None
    policy: RetrievalPolicy


def _pack(
    plan: QueryPlan,
    candidates: tuple[RankedChunk, ...],
    **options: Unpack[_PackOptions],
) -> GenerationEvidencePack:
    return build_generation_evidence_pack(
        query_plan=plan,
        root_evidence=options.get("root", ()),
        atom_evidence=options.get("atom", ()),
        ranked_candidates=candidates,
        groups=options.get("groups", ()),
        links=options.get("links", ()),
        request=options.get("request") or _request(),
        active_revision_id=f"irev_{'c' * 32}",
        excluded_document_ids=(),
        policy=options.get("policy", RetrievalPolicy()),
    )


def test_table_action_reenters_pack_from_bounded_rerank_pool() -> None:
    """原问中的职责主体直接命中表格原文时，旧装配配额不应造成空包。"""
    source = "甲团队负责编制项目实施计划并提交审核"
    candidate = make_ranked_chunk(1, source, role=ChunkRole.TABLE)
    chunk = candidate.hydrated.chunk
    span = chunk.source_spans[0]
    path = ("body", "tbl:1", "tr:1", "tc:1", "p:1")
    table_span = span.model_copy(
        update={
            "structural_path": path,
            "source_anchor": span.source_anchor.model_copy(
                update={"structural_path": path}
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"source_spans": (table_span,)}
                    )
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="团队任务",
        relation="职责",
        answer_shape=AtomAnswerShape.DUTIES,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "甲团队负责哪些工作？",
            "resolved_root_query": "甲团队负责哪些工作？",
        }
    )
    pack = _pack(plan, (candidate,))
    assert [item.citation_text for item in pack.evidence] == [source]


def test_procedure_overlap_does_not_displace_root_evidence() -> None:
    """流程问句的词片重合不应先占据表格职责配额。"""
    distractor = make_ranked_chunk(
        1, "快验类研发资源由市场拓展中心联合评审", role=ChunkRole.TABLE
    )
    chunk = distractor.hydrated.chunk
    span = chunk.source_spans[0]
    path = ("body", "tbl:1", "tr:1", "tc:1", "p:1")
    table_span = span.model_copy(
        update={
            "structural_path": path,
            "source_anchor": span.source_anchor.model_copy(
                update={"structural_path": path}
            ),
        }
    )
    distractor = distractor.model_copy(
        update={
            "hydrated": distractor.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"source_spans": (table_span,)}
                    )
                }
            )
        }
    )
    root, evidence = _item(2, "经例会评审通过，以会议纪要为准启动。")
    atom = QueryAtom(
        atom_id="A1",
        target="需求快验",
        relation="输入和启动条件",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    plan = _plan(atom).model_copy(
        update={
            "original_query": "想走快验，手头先得有什么，满足啥才能开？",
            "resolved_root_query": "想走快验，手头先得有什么，满足啥才能开？",
        }
    )
    pack = _pack(
        plan,
        (distractor, root),
        root=(evidence,),
        policy=RetrievalPolicy(
            generation_per_document_cap=1,
            generation_max_ordinary_items=1,
        ),
    )
    assert [item.chunk_id for item in pack.evidence] == [
        root.hydrated.chunk.chunk_id
    ]


def test_direct_support_mismatch_remains_generation_candidate() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "answer_support": {
                        "status": "SUPPORTED",
                        "query_target": "检修记录",
                        "requested_relation_or_attribute": "归档期限",
                    }
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检修资料",
        relation="保存时限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    link = AtomCandidateLink(
        atom_id="A1",
        chunk_id=candidate.hydrated.chunk.chunk_id,
        channels=("lexical",),
        best_rank=1,
        score=1.0,
    )
    pack = _pack(_plan(atom), (candidate,), atom=(evidence,), links=(link,))
    assert len(pack.entries) == 1
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1",)),)
    assert EvidenceAdmissionReason.TARGET_SOFT_MISMATCH in (
        pack.entries[0].soft_signals
    )


def test_root_candidate_without_atom_provenance_is_available() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="期限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert pack.evidence[0].citation_text == evidence.citation_text
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1",)),)
    assert EvidenceAdmissionReason.ROOT_RETRIEVAL in (
        pack.entries[0].soft_signals
    )
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.MISSING),)
    )
    assert pack.pre_generation_availability(matrix) == {
        "A1": "EVIDENCE_AVAILABLE"
    }


def test_compound_question_keeps_prior_stage_within_document_cap() -> None:
    """同文档的高排名片段不能耗尽前序阶段的生成证据名额。"""
    seeds = tuple(
        make_ranked_chunk(number, f"立项阶段{number}。").model_copy(
            update={"rerank_rank": number}
        )
        for number in range(1, 5)
    )
    predecessors = tuple(
        make_ranked_chunk(
            number,
            text,
            role=ChunkRole.LIST,
            neighbor_group_id=f"stage-{number}",
        ).model_copy(
            update={
                "expansion_reason": "SECTION_PREDECESSOR",
                "expansion_seed_ids": (seeds[0].hydrated.chunk.chunk_id,),
            }
        )
        for number, text in (
            (5, "立项准备阶段需编制项目材料。"),
            (6, "项目立项包括准备、申报、审核、决策和系统立项。"),
        )
    )
    first = predecessors[0]
    original_span = first.hydrated.chunk.source_spans[0]
    assert original_span.source_anchor is not None
    split_at = 4
    opening = original_span.model_copy(
        update={
            "chunk_end_char": split_at,
            "source_end_char": split_at,
            "source_anchor": original_span.source_anchor.model_copy(
                update={"source_end_char": split_at}
            ),
        }
    )
    remainder = original_span.model_copy(
        update={
            "chunk_start_char": split_at,
            "source_start_char": split_at,
            "source_anchor": original_span.source_anchor.model_copy(
                update={"source_start_char": split_at}
            ),
        }
    )
    first = first.model_copy(
        update={
            "hydrated": first.hydrated.model_copy(
                update={
                    "chunk": first.hydrated.chunk.model_copy(
                        update={"source_spans": (opening, remainder)}
                    )
                }
            )
        }
    )
    predecessors = (first, predecessors[1])
    candidates = (*seeds, *predecessors)
    root = tuple(
        _evidence_item(
            candidate,
            candidate.hydrated.chunk.source_spans[0],
            candidate.hydrated.chunk.citation_text[
                : candidate.hydrated.chunk.source_spans[0].chunk_end_char
            ],
            "S1",
        )
        for candidate in candidates
    )
    root += (
        _evidence_item(
            first,
            remainder,
            first.hydrated.chunk.citation_text[split_at:],
            "S2",
        ),
    )
    atoms = (
        QueryAtom(
            atom_id="A1",
            target="立项",
            relation="材料",
            answer_shape=AtomAnswerShape.FACT,
        ),
        QueryAtom(
            atom_id="A2",
            target="立项",
            relation="步骤",
            answer_shape=AtomAnswerShape.FACT,
        ),
    )
    shadow_member = predecessors[1].model_copy(
        update={"expansion_reason": "STRUCTURE_CONTINUITY"}
    )
    shadow_group = build_evidence_groups(
        (shadow_member,),
        max_groups=1,
        max_member_chunks=8,
        rerank_text_char_limit=2400,
    )[0]

    pack = _pack(
        _plan(*atoms),
        candidates,
        root=root,
        groups=(shadow_group,),
        policy=RetrievalPolicy(generation_per_document_cap=4),
    )

    assert {item.hydrated.chunk.chunk_id for item in predecessors} <= {
        item.chunk_id for item in pack.evidence
    }


def test_compound_question_keeps_distinct_reranked_primary_prose() -> None:
    """旧装配器漏掉的高排名正文仍可按原 SourceSpan 补入证据包。"""
    texts = (
        "跨年领到证书的，在领取年份报销并占用当年额度。",
        "报销前须向部门提交认证申请并获得审批。",
        "一、报销说明",
        "二、认证费用",
        "其他制度中的跨年报销规定不属于这份资料。",
        "员工通过认证取得证书后，在额度内按规定时间集中报销。",
    )
    candidates = tuple(
        make_ranked_chunk(
            number,
            text,
            role=ChunkRole.LIST if number in {1, 3, 4} else ChunkRole.TEXT,
            document_number=3 if number == 5 else 2,
        ).model_copy(update={"rerank_rank": number})
        for number, text in enumerate(texts, 1)
    )
    root = tuple(
        _evidence_item(
            candidate,
            candidate.hydrated.chunk.source_spans[0],
            candidate.hydrated.chunk.citation_text,
            "S1",
        )
        for candidate in candidates[:4]
    )
    atoms = (
        QueryAtom(
            atom_id="A1",
            target="认证费",
            relation="跨年报销年份",
            answer_shape=AtomAnswerShape.FACT,
        ),
        QueryAtom(
            atom_id="A2",
            target="认证费",
            relation="报销条件",
            answer_shape=AtomAnswerShape.FACT,
        ),
    )

    pack = _pack(
        _plan(*atoms),
        candidates,
        root=root,
        policy=RetrievalPolicy(generation_per_document_cap=4),
    )

    ids = {item.chunk_id for item in pack.evidence}
    required = {
        candidates[index].hydrated.chunk.chunk_id for index in (0, 1, 5)
    }
    assert required <= ids
    assert candidates[4].hydrated.chunk.chunk_id not in ids
    assert all(item.source_spans[0].is_citable for item in pack.evidence)


def test_ordinary_chunk_keeps_adjacent_source_paragraph() -> None:
    """同块首选段落提到供应商时，前一段的时限仍可入包。"""
    first = "发布采购文件到应答截止时间，不得少于3日。"
    second = "潜在供应商在截止时间前提交应答。"
    candidate = make_ranked_chunk(1, first + "\n" + second)
    original = candidate.hydrated.chunk.source_spans[0]
    assert original.source_anchor is not None
    earlier = original.model_copy(
        update={
            "chunk_end_char": len(first),
            "source_end_char": len(first),
            "source_anchor": original.source_anchor.model_copy(
                update={"source_end_char": len(first)}
            ),
        }
    )
    later = original.model_copy(
        update={
            "node_id": f"node_{2:032x}",
            "structural_path": ("body", "p:2"),
            "chunk_start_char": len(first) + 1,
            "chunk_end_char": len(first) + 1 + len(second),
            "source_end_char": len(second),
            "source_anchor": original.source_anchor.model_copy(
                update={
                    "structural_path": ("body", "p:2"),
                    "ordinal": 2,
                    "paragraph_index": 2,
                    "source_end_char": len(second),
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": candidate.hydrated.chunk.model_copy(
                        update={
                            "source_spans": (earlier, later),
                            "role": ChunkRole.LIST,
                        }
                    )
                }
            )
        }
    )
    evidence = _evidence_item(candidate, later, second, "S1")
    atom = QueryAtom(
        atom_id="A1",
        target="供应商",
        relation="应答期限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert {item.citation_text for item in pack.evidence} == {first, second}


def test_source_node_closure_keeps_cross_chunk_continuation() -> None:
    """原文段落跨块时，不能只把逗号前的片段交给生成器。"""
    texts = (
        "研发人员记录项目工时，",
        "人力资源岗按工时归集人工成本，",
        "财务岗审核入账。",
    )
    candidates: list[RankedChunk] = []
    offset = 0
    for number, text in enumerate(texts, start=1):
        candidate = make_ranked_chunk(number, text)
        span = candidate.hydrated.chunk.source_spans[0]
        anchor = span.source_anchor
        assert anchor is not None
        adjusted = span.model_copy(
            update={
                "node_id": f"node_{1:032x}",
                "source_anchor": anchor.model_copy(update={"ordinal": 1}),
                "source_start_char": offset,
                "source_end_char": offset + len(text),
            }
        )
        candidates.append(
            candidate.model_copy(
                update={
                    "hydrated": candidate.hydrated.model_copy(
                        update={
                            "chunk": candidate.hydrated.chunk.model_copy(
                                update={"source_spans": (adjusted,)}
                            )
                        }
                    )
                }
            )
        )
        offset += len(text)
    first = candidates[0]
    evidence = _evidence_item(
        first, first.hydrated.chunk.source_spans[0], texts[0], "S1"
    )
    atom = QueryAtom(
        atom_id="A1",
        target="研发人工成本核算",
        relation="参与步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )

    pack = _pack(
        _plan(atom),
        tuple(candidates),
        root=(evidence,),
        policy=RetrievalPolicy(
            generation_per_document_cap=1,
            generation_max_ordinary_items=1,
        ),
    )

    assert {item.citation_text for item in pack.evidence} == set(texts)
    assert pack.per_atom_candidate_support_ids == (
        ("A1", tuple(item.support_id for item in pack.evidence)),
    )


def test_table_cell_keeps_same_node_continuation_across_chunks() -> None:
    """同一表格单元格跨块时，生成包保留完整原文片段。"""
    texts = ("甲团队负责系统联调，", "解决接口兼容性问题。")
    node_id = f"node_{71:032x}"
    table_node = f"node_{72:032x}"
    path = ("body", "tbl:1", "tr:1", "tc:1", "p:1")
    candidates: list[RankedChunk] = []
    offset = 0
    for number, source in enumerate(texts, 1):
        candidate = make_ranked_chunk(number, source, role=ChunkRole.TABLE)
        chunk = candidate.hydrated.chunk
        span = chunk.source_spans[0]
        assert span.source_anchor is not None
        adjusted = span.model_copy(
            update={
                "node_id": node_id,
                "structural_path": path,
                "source_start_char": offset,
                "source_end_char": offset + len(source),
                "source_anchor": span.source_anchor.model_copy(
                    update={"structural_path": path, "ordinal": 1}
                ),
            }
        )
        metadata = freeze_json_object(
            {
                "atoms": [
                    {
                        "metadata": {
                            "row_index": 1,
                            "table_node_id": table_node,
                            "cell_source_node_ids": {"1": [node_id]},
                        }
                    }
                ]
            }
        )
        candidate = candidate.model_copy(
            update={
                "hydrated": candidate.hydrated.model_copy(
                    update={
                        "chunk": chunk.model_copy(
                            update={
                                "source_spans": (adjusted,),
                                "metadata": metadata,
                            }
                        )
                    }
                )
            }
        )
        candidates.append(candidate)
        offset += len(source)
    first = candidates[0]
    evidence = _evidence_item(
        first, first.hydrated.chunk.source_spans[0], texts[0], "S1"
    )
    atom = QueryAtom(
        atom_id="A1",
        target="甲团队",
        relation="职责",
        answer_shape=AtomAnswerShape.DUTIES,
    )
    pack = _pack(
        _plan(atom),
        tuple(candidates),
        root=(evidence,),
        policy=RetrievalPolicy(
            generation_per_document_cap=1,
            generation_max_ordinary_items=1,
        ),
    )
    assert {item.citation_text for item in pack.evidence} == set(texts)


def test_table_row_closure_keeps_mapped_duration_without_other_row() -> None:
    """表格行的合并单元格与时限须同包，未映射的兄弟行不可混入。"""
    report = "电话及邮件方式报送信息安全部。"
    duration = "30分钟"
    other_row = "1小时"
    text = f"{report} | {duration} | {other_row}"
    candidate = make_ranked_chunk(1, text, role=ChunkRole.TABLE)
    original = candidate.hydrated.chunk.source_spans[0]
    anchor = original.source_anchor
    assert anchor is not None
    table_node = f"node_{90:032x}"
    report_node = f"node_{91:032x}"
    duration_node = f"node_{92:032x}"
    other_node = f"node_{93:032x}"

    def cell_span(
        node_id: str, row: int, column: int, start: int, value: str
    ) -> SourceSpan:
        path = ("body", "tbl:1", f"tr:{row}", f"tc:{column}")
        return original.model_copy(
            update={
                "node_id": node_id,
                "structural_path": path,
                "chunk_start_char": start,
                "chunk_end_char": start + len(value),
                "source_start_char": 0,
                "source_end_char": len(value),
                "source_anchor": anchor.model_copy(
                    update={
                        "structural_path": path,
                        "ordinal": row * 10 + column,
                        "row_index": row,
                        "column_index": column,
                        "source_start_char": 0,
                        "source_end_char": len(value),
                    }
                ),
            }
        )

    spans = (
        cell_span(report_node, 1, 1, 0, report),
        cell_span(duration_node, 2, 2, len(report) + 3, duration),
        cell_span(other_node, 3, 2, len(report) + len(duration) + 6, other_row),
    )
    chunk = candidate.hydrated.chunk.model_copy(
        update={
            "source_spans": spans,
            "metadata": freeze_json_object(
                {
                    "atoms": [
                        {
                            "metadata": {
                                "row_index": 2,
                                "table_node_id": table_node,
                                "cell_source_node_ids": {
                                    "1": [report_node],
                                    "2": [duration_node],
                                },
                            }
                        }
                    ]
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )
    evidence = _evidence_item(candidate, spans[0], report, "S1")
    atom = QueryAtom(
        atom_id="A1",
        target="信息安全事件",
        relation="报告方式和时限",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )

    pack = _pack(_plan(atom), (candidate,), root=(evidence,))

    assert {item.citation_text for item in pack.evidence} == {report, duration}
    assert all(item.source_spans[0].is_citable for item in pack.evidence)
    assert {
        (
            dict(item.metadata).get("table_logical_node_id"),
            dict(item.metadata).get("table_logical_row_index"),
        )
        for item in pack.evidence
    } == {(table_node, 2)}
    assert "table_logical_node_id" not in dict(
        _evidence_item(candidate, spans[2], other_row, "S9").metadata
    )
    assert (
        pack.structural_sibling_observation(())[
            "structural_sibling_pollution_count"
        ]
        == 0
    )


def test_explicit_source_mismatch_is_hard_rejected() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="期限",
        answer_shape=AtomAnswerShape.DURATION,
        source_qualifier="指定制度.docx",
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert not pack.entries
    assert pack.rejected_entries[0].admission_status is (
        EvidenceAdmissionStatus.REJECTED_HARD
    )
    assert EvidenceAdmissionReason.EXPLICIT_SOURCE_MISMATCH in (
        pack.rejected_entries[0].hard_reject_reasons
    )
    assert pack.hard_rejected_sources == (
        {
            "document_version_id": evidence.document_version_id,
            "chunk_id": evidence.chunk_id,
            "node_ids": (evidence.source_spans[0].node_id,),
            "reasons": ("EXPLICIT_SOURCE_MISMATCH",),
        },
    )


def test_non_citable_and_inactive_version_are_hard_rejected() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="期限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    invalid = evidence.model_copy(
        update={
            "document_version_id": f"dver_{'f' * 32}",
            "source_spans": (
                evidence.source_spans[0].model_copy(
                    update={"is_citable": False}
                ),
            ),
        }
    )
    pack = _pack(_plan(atom), (candidate,), root=(invalid,))
    assert not pack.entries
    assert set(pack.rejected_entries[0].hard_reject_reasons) == {
        EvidenceAdmissionReason.NON_CITABLE,
        EvidenceAdmissionReason.INACTIVE_VERSION,
    }


def test_partial_group_evidence_is_admitted_for_limited_answer() -> None:
    candidate, evidence = _item(1, "第一项：检查设备。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": "egrp_partial",
                    "evidence_group_type": "LIST_GROUP",
                    "group_complete": False,
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert len(pack.entries) == 1
    assert pack.entries[0].admission_status is (
        EvidenceAdmissionStatus.ADMITTED_STRUCTURED_PARTIAL
    )
    assert pack.partial_group_ids == ("egrp_partial",)


def test_direct_single_value_literal_contradiction_is_hard_rejected() -> None:
    candidate, evidence = _item(1, "检修记录应在5天内归档。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "answer_support": {
                        "status": "SUPPORTED",
                        "query_target": "检修记录",
                        "requested_relation_or_attribute": "归档期限",
                    }
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="归档期限",
        answer_shape=AtomAnswerShape.DURATION,
        constraints=(
            QueryConstraint(kind=AtomConstraintKind.DURATION, value="3天"),
        ),
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert not pack.entries
    assert EvidenceAdmissionReason.HARD_LITERAL_CONTRADICTION in (
        pack.rejected_entries[0].hard_reject_reasons
    )


def _complete_group(
    members: tuple[RankedChunk, ...],
) -> GroupCandidate:
    first = members[0].hydrated.chunk
    source_maps = tuple(
        GroupSourceMap(
            chunk_id=member.hydrated.chunk.chunk_id,
            citation_text=member.hydrated.chunk.citation_text,
            source_spans=member.hydrated.chunk.source_spans,
        )
        for member in members
    )
    return GroupCandidate(
        group=EvidenceGroup(
            group_id=f"egrp_{'a' * 32}",
            kind=EvidenceGroupKind.LIST_GROUP,
            document_id=first.version.document_id,
            document_version_id=first.version.document_version_id,
            index_revision_id=first.index_revision_id,
            section_id=first.section_id,
            display_name="fixture.docx",
            heading_path=("检查步骤",),
            member_chunk_ids=tuple(item.chunk_id for item in source_maps),
            member_source_maps=source_maps,
            member_ranks=tuple(range(1, len(members) + 1)),
            group_text_for_model="\n".join(
                member.hydrated.chunk.citation_text for member in members
            ),
            complete=True,
            token_cost=sum(
                member.hydrated.chunk.token_count for member in members
            ),
        ),
        members=members,
        rerank_text="\n".join(
            member.hydrated.chunk.citation_text for member in members
        ),
    )


def test_complete_group_fills_real_member_spans() -> None:
    first, evidence = _item(1, "检查步骤包括：")
    second, _second_evidence = _item(2, "第一步：检查设备。")
    group = _complete_group((first, second))
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(
        _plan(atom),
        (first,),
        root=(evidence,),
        groups=(group,),
    )
    assert len(pack.entries) == 2
    assert pack.complete_group_ids == (group.group_id,)
    assert {item.chunk_id for item in pack.evidence} == {
        first.hydrated.chunk.chunk_id,
        second.hydrated.chunk.chunk_id,
    }
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1", "S2")),)
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.PARTIAL),)
    )
    assert pack.pre_generation_availability(matrix) == {
        "A1": "STRUCTURED_COMPLETE"
    }
    assert pack.structural_sibling_observation((group,)) == {
        "structural_sibling_pollution_count": 0,
        "structural_sibling_observation_status": "COMPLETE",
        "structural_sibling_unknown_group_count": 0,
        "structural_sibling_unknown_structural_count": 0,
        "structural_sibling_unknown_table_coordinate_count": 0,
        "structural_sibling_multi_row_table_count": 0,
        "structural_sibling_observation_scope": (
            "ADMITTED_STRUCTURAL_GROUP_AND_TABLE_ROW"
        ),
    }


def test_complete_group_precedes_node_continuation() -> None:
    """来源续片不能占掉已命中完整步骤组的唯一剩余名额。"""
    first, evidence = _item(1, "检查事项包括，")
    continuation, _ = _item(2, "附加说明。")
    member, _ = _item(3, "第一步：检查设备。")
    first_span = first.hydrated.chunk.source_spans[0]
    next_span = continuation.hydrated.chunk.source_spans[0]
    assert first_span.source_anchor is not None
    assert next_span.source_anchor is not None
    continued = next_span.model_copy(
        update={
            "node_id": first_span.node_id,
            "structural_path": first_span.structural_path,
            "source_start_char": len(first.hydrated.chunk.citation_text),
            "source_end_char": len(first.hydrated.chunk.citation_text)
            + len(continuation.hydrated.chunk.citation_text),
            "source_anchor": next_span.source_anchor.model_copy(
                update={
                    "ordinal": first_span.source_anchor.ordinal,
                    "structural_path": first_span.structural_path,
                }
            ),
        }
    )
    continuation = continuation.model_copy(
        update={
            "hydrated": continuation.hydrated.model_copy(
                update={
                    "chunk": continuation.hydrated.chunk.model_copy(
                        update={"source_spans": (continued,)}
                    )
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="检查事项",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    group = _complete_group((first, member))
    pack = _pack(
        _plan(atom),
        (first, continuation),
        root=(evidence,),
        groups=(group,),
        policy=RetrievalPolicy(
            generation_per_document_cap=1,
            generation_max_ordinary_items=1,
            generation_max_group_items=1,
        ),
    )
    assert group.group_id in pack.complete_group_ids
    assert {item.chunk_id for item in pack.evidence} == {
        first.hydrated.chunk.chunk_id,
        member.hydrated.chunk.chunk_id,
    }


def test_later_complete_group_keeps_its_source_spans() -> None:
    """多个完整组同时入包时，末组仍须包含行内的全部事实。"""
    groups: list[GroupCandidate] = []
    first_members: list[RankedChunk] = []
    first_evidence: list[EvidenceItem] = []
    for group_index in range(3):
        members = tuple(
            make_ranked_chunk(
                group_index * 7 + member_index + 1,
                f"第 {group_index + 1} 组，第 {member_index + 1} 项。",
                document_number=group_index + 1,
            )
            for member_index in range(7)
        )
        group = _complete_group(members)
        groups.append(
            replace(
                group,
                group=group.group.model_copy(
                    update={"group_id": f"egrp_{group_index + 1:032x}"}
                ),
            )
        )
        first_members.append(members[0])
        first_evidence.append(
            _evidence_item(
                members[0],
                members[0].hydrated.chunk.source_spans[0],
                members[0].hydrated.chunk.citation_text,
                "S1",
            )
        )
    atom = QueryAtom(
        atom_id="A1",
        target="第三组",
        relation="全部事实",
        answer_shape=AtomAnswerShape.ENUMERATION,
    )
    pack = _pack(
        _plan(atom),
        tuple(first_members),
        root=tuple(first_evidence),
        groups=tuple(groups),
    )
    assert len(pack.complete_group_ids) == 3
    assert {item.chunk_id for item in pack.evidence} >= {
        member.hydrated.chunk.chunk_id for member in groups[-1].members
    }


def test_unknown_group_provenance_is_only_partially_observed() -> None:
    candidate, evidence = _item(1, "第一项：检查设备。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": "egrp_unknown",
                    "evidence_group_type": "LIST_GROUP",
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    observation = pack.structural_sibling_observation(())
    assert observation["structural_sibling_pollution_count"] == 0
    assert observation["structural_sibling_observation_status"] == "PARTIAL"
    assert observation["structural_sibling_unknown_group_count"] == 1


def test_ungrouped_table_row_coordinates_are_observable() -> None:
    candidate = make_ranked_chunk(1, "机型", role=ChunkRole.TABLE)
    original_span = candidate.hydrated.chunk.source_spans[0]
    path = ("body", "tbl:2", "tr:2", "tc:0")
    anchor = original_span.source_anchor.model_copy(
        update={
            "structural_path": path,
            "table_index": 2,
            "row_index": 2,
            "cell_index": 0,
        }
    )
    span = original_span.model_copy(
        update={"structural_path": path, "source_anchor": anchor}
    )
    chunk = candidate.hydrated.chunk.model_copy(
        update={
            "source_spans": (span,),
            "metadata": freeze_json_object(
                {
                    "atoms": [
                        {
                            "metadata": {
                                "table_node_id": f"node_{'a' * 32}",
                                "row_index": 2,
                            }
                        }
                    ]
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )
    evidence = _evidence_item(candidate, span, "机型", "S1")
    atom = QueryAtom(
        atom_id="A1",
        target="机型",
        relation="名称",
        answer_shape=AtomAnswerShape.FACT,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert len(pack.entries) == 1
    assert pack.entries[0].table_node_id == f"node_{'a' * 32}"
    assert pack.entries[0].table_row_index == 2
    observation = pack.structural_sibling_observation(())
    assert observation["structural_sibling_pollution_count"] == 0
    assert observation["structural_sibling_observation_status"] == "COMPLETE"
    assert observation["structural_sibling_unknown_structural_count"] == 0
    other_row = replace(pack.entries[0], support_id="S2", table_row_index=3)
    multi_row = replace(pack, entries=(*pack.entries, other_row))
    multi_observation = multi_row.structural_sibling_observation(())
    assert (
        multi_observation["structural_sibling_observation_status"] == "COMPLETE"
    )
    assert multi_observation["structural_sibling_multi_row_table_count"] == 1
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.PARTIAL),)
    )
    assert pack.pre_generation_availability(matrix) == {
        "A1": "STRUCTURED_PARTIAL"
    }


def test_sibling_group_identity_conflict_is_hard_rejected() -> None:
    first, _first_evidence = _item(1, "检查步骤包括：")
    second, _second_evidence = _item(2, "第一步：检查设备。")
    sibling, evidence = _item(3, "别的流程：报告异常。")
    group = _complete_group((first, second))
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": group.group_id,
                    "evidence_group_type": "LIST_GROUP",
                    "group_complete": True,
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(
        _plan(atom),
        (sibling,),
        root=(evidence,),
        groups=(group,),
    )
    assert not pack.entries
    assert EvidenceAdmissionReason.STRUCTURAL_SIBLING_CONFLICT in (
        pack.rejected_entries[0].hard_reject_reasons
    )


def test_table_cross_row_and_scope_mismatch_are_hard_rejected() -> None:
    candidate, evidence = _item(1, "表格数值：3天。")
    span = evidence.source_spans[0]
    evidence = evidence.model_copy(
        update={
            "table_context": True,
            "source_spans": (
                span.model_copy(
                    update={"structural_path": ("body", "tbl:1", "tr:1")}
                ),
                span.model_copy(
                    update={"structural_path": ("body", "tbl:1", "tr:2")}
                ),
            ),
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="表格数值",
        relation="时限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    wrong_scope = SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'f' * 32}",
            knowledge_base_id=f"kb_{'b' * 32}",
        ),
        text="表格数值的时限是多少？",
    )
    pack = _pack(
        _plan(atom),
        (candidate,),
        root=(evidence,),
        request=wrong_scope,
    )
    assert set(pack.rejected_entries[0].hard_reject_reasons) == {
        EvidenceAdmissionReason.TABLE_ROW_CONFLICT,
        EvidenceAdmissionReason.SCOPE_MISMATCH,
    }


def test_catalog_entry_cannot_supply_template_body() -> None:
    candidate, evidence = _item(1, "模板目录列有设备登记表。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {"evidence_group_type": "CATALOG_ENTRY"}
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备登记表",
        relation="正文内容",
        answer_shape=AtomAnswerShape.FACT,
    )
    pack = _pack(
        _plan(atom),
        (candidate,),
        root=(evidence,),
        request=_request("设备登记表的正文内容是什么？"),
    )
    assert not pack.entries
    assert EvidenceAdmissionReason.TEMPLATE_BODY_UNAVAILABLE in (
        pack.rejected_entries[0].hard_reject_reasons
    )


def test_catalog_entry_cannot_validate_placeholder_as_requirement() -> None:
    candidate, evidence = _item(1, "模板目录列有会议纪要模板。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {"evidence_group_type": "CATALOG_ENTRY"}
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="会议纪要模板",
        relation="占位内容是否正式要求",
        answer_shape=AtomAnswerShape.FACT,
    )
    pack = _pack(
        _plan(atom),
        (candidate,),
        root=(evidence,),
        request=_request("会议纪要模板的占位或示例能当正式要求吗？"),
    )
    assert not pack.entries
    assert EvidenceAdmissionReason.TEMPLATE_BODY_UNAVAILABLE in (
        pack.rejected_entries[0].hard_reject_reasons
    )


def test_catalog_title_certificate_cannot_validate_template_body() -> None:
    """目录条目即使以普通检索结果入包，也不能证明模板正文。"""
    title = "需求阶段-需求变更评审会议纪要模板"
    candidate, evidence = _item(
        1,
        f"模板目录项：{title}（模板）。模板正文未入库；请参考原始模板。",
    )
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "answer_support": {
                        "status": "SUPPORTED",
                        "support_reason": "CATALOG_TITLE_EXISTS",
                    }
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target=title,
        relation="占位内容是否正式要求",
        answer_shape=AtomAnswerShape.FACT,
    )
    pack = _pack(
        _plan(atom),
        (candidate,),
        root=(evidence,),
        request=_request("会议纪要模板的占位或示例能当正式要求吗？"),
    )
    assert not pack.entries
    assert EvidenceAdmissionReason.TEMPLATE_BODY_UNAVAILABLE in (
        pack.rejected_entries[0].hard_reject_reasons
    )
