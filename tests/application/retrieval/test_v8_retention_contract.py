"""V8 两级融合的通道来源、裁剪前种子和保护合同回归。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_app.application.retrieval.fusion import reciprocal_rank_fusion
from rag_app.application.retrieval.hydration import CandidateHydrator
from rag_app.application.retrieval.observations import candidate_observation
from rag_app.application.retrieval.planner import QueryPlanner
from rag_app.application.retrieval.query_plan_retrieval import (
    QueryUnitRetrieval,
    fuse_query_units,
)
from rag_app.application.retrieval.reranking import (
    CircuitAwareReranker,
    _select_input,
)
from rag_app.application.retrieval.retention import structural_seed_ids
from rag_app.application.retrieval.service import RetrievalService
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ChannelHit,
    ChunkRole,
    FusedCandidate,
    QueryVariant,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.core.models.search import RetrievalOrigin
from rag_app.core.policies import EgressPolicy
from tests.application.retrieval.helpers import make_ranked_chunk
from tests.application.retrieval.test_reranking import _Reranker
from tests.application.retrieval.test_root_atom_retrieval import (
    _REVISION,
    _SCOPE,
    _hit,
    _unit,
)


def test_two_level_fusion_hydration_preserves_rare_channel_for_rerank() -> None:
    """ROOT/A1 的评分解释不能冒充真实 lexical/dense 通道。"""
    units = tuple(
        QueryUnitRetrieval(
            _unit(unit_id, (), atom_count=1).unit,
            {
                "lexical": tuple(_hit(n, n) for n in range(1, 25)),
                "structural": tuple(
                    _hit(n, n, channel="structural") for n in range(1, 25)
                ),
                "dense:primary": tuple(
                    _hit(n, n - 100, channel="dense:primary")
                    for n in range(101, 109)
                ),
            },
        )
        for unit_id in ("ROOT", "A1")
    )
    outcome = fuse_query_units(
        units, revision_id=_REVISION, policy=RetrievalPolicy()
    )
    hydrated_by_id = {}
    for candidate in outcome.candidates:
        row = make_ranked_chunk(
            int(candidate.chunk_id.removeprefix("chunk_"), 16), "合成原文。"
        ).hydrated
        chunk = row.chunk.model_copy(
            update={
                "version": row.chunk.version.model_copy(
                    update={
                        "document_id": candidate.document_id,
                        "document_version_id": candidate.document_version_id,
                    }
                ),
                "section_id": candidate.section_id,
                "content_sha256": candidate.content_sha256,
            }
        )
        hydrated_by_id[candidate.chunk_id] = row.model_copy(
            update={"chunk": chunk}
        )
    source = Mock()
    source.hydrate_chunks.side_effect = lambda _snapshot, ids: tuple(
        hydrated_by_id[key] for key in ids
    )
    hydrated = CandidateHydrator(source).hydrate(Mock(), outcome.candidates)
    provider = _Reranker(equal=True)

    CircuitAwareReranker(provider).rerank(
        "启动需要什么材料",
        hydrated,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=10,
    )

    assert provider.request is not None
    sent_ids = {item[0] for item in provider.request.candidates}
    assert any(f"chunk_{n:032x}" in sent_ids for n in range(101, 109))
    assert all(
        {item.channel for item in candidate.contributions} == {"ROOT", "A1"}
        for candidate in outcome.candidates
    )


def test_root_lexical_seed_is_reserved_before_unit_fusion_truncation() -> None:
    """三路重复命中填满 32 位时，原问前三词面种子仍有保留名额。"""
    root = QueryUnitRetrieval(
        _unit("ROOT", (), atom_count=1).unit,
        {
            "lexical": tuple(_hit(n, n) for n in (1, 2, 3)),
            **{
                channel: tuple(
                    _hit(n, n - 10, channel=channel) for n in range(11, 44)
                )
                for channel in ("dense:primary", "structural", "exact")
            },
        },
    )
    outcome = fuse_query_units(
        (root,), revision_id=_REVISION, policy=RetrievalPolicy()
    )

    assert {f"chunk_{n:032x}" for n in (1, 2, 3)} <= set(outcome.seed_chunk_ids)
    assert len(outcome.candidates) == 32


def test_pre_fused_path_preserves_the_same_structural_seed_contract() -> None:
    """pre_fused 的结构保护集合必须与普通路径一致。"""
    structural = _hit(29, 1, channel="structural").model_copy(
        update={"match_type": "STRUCTURAL_TABLE_ROW"}
    )
    channels = {"lexical": (_hit(1, 1),), "structural": (structural,)}
    fused = reciprocal_rank_fusion(channels, expected_revision_id=_REVISION)
    service = object.__new__(RetrievalService)
    service._policy = RetrievalPolicy()
    service._hydrator = Mock()
    service._hydrator.hydrate.return_value = ()
    service._trace = Mock()
    service._egress = EgressPolicy()
    service._reranker = Mock()
    service._reranker.rerank.side_effect = RuntimeError("captured-rerank")
    snapshot = SimpleNamespace(
        excluded_document_ids=(),
        revision=SimpleNamespace(index_revision_id=_REVISION),
    )
    analysis = _unit("ROOT", (), atom_count=1).unit.analysis
    plan = QueryPlanner().plan(
        analysis,
        (
            QueryVariant(
                text="启动需要什么材料",
                kind="original",
                identity="sha256:" + "1" * 64,
            ),
        ),
        service._policy,
    )
    request = SearchRequest(scope=_SCOPE, text="启动需要什么材料")
    with pytest.raises(RuntimeError, match="captured-rerank"):
        service._rank_and_select(
            request=request,
            snapshot=snapshot,
            analysis=analysis,
            plan=plan,
            channel_hits=channels,
            selected_slot=None,
            trace_id=f"trace_{'9' * 32}",
            provider_calls=[],
            degraded=[],
            stage_timings=[],
            retrieval_phase="original",
            pre_fused=fused,
        )

    assert (
        structural.chunk_id
        in service._reranker.rerank.call_args.kwargs["required_candidate_ids"]
    )


def test_variant_origins_remain_observable_without_duplicate_rrf_votes() -> (
    None
):
    original = _hit(1, 2)
    rewrite = _hit(1, 1, channel="lexical:rewrite")
    fused = reciprocal_rank_fusion(
        {"lexical": (original,), "lexical:rewrite": (rewrite,)},
        expected_revision_id=_REVISION,
    )
    assert fused[0].score == pytest.approx(1 / 61)
    assert len(fused[0].contributions) == 1
    assert {origin.variant_id for origin in fused[0].retrieval_origins} == {
        "lexical",
        "lexical:rewrite",
    }
    assert {origin.native_rank for origin in fused[0].retrieval_origins} == {
        1,
        2,
    }


def test_structural_quota_is_bounded_and_retains_another_source() -> None:
    hits = tuple(
        _hit(
            number,
            number,
            channel="structural",
            document_number=1 if number <= 25 else 2,
        ).model_copy(update={"match_type": "STRUCTURAL_TABLE_ROW"})
        for number in range(1, 31)
    )
    selected = structural_seed_ids({"structural": hits})
    assert len(selected) == 6
    assert f"chunk_{26:032x}" in selected
    assert selected == structural_seed_ids(
        {"structural": tuple(reversed(hits))}
    )


def test_unit_protection_cannot_evict_the_last_rare_channel() -> None:
    candidates = tuple(
        make_ranked_chunk(number, f"同文档片段 {number}").model_copy(
            update={
                "retention_reasons": (f"UNIT_SEED:A{(number - 1) % 4 + 1}",)
            }
        )
        for number in range(1, 31)
    )
    rare = make_ranked_chunk(
        99, "另一文档的语义证据", channel="dense:primary", document_number=8
    )
    provider = _Reranker(equal=True)
    outcome = CircuitAwareReranker(provider).rerank(
        "准备事项",
        (*candidates, rare),
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=24,
        required_candidate_ids=frozenset(
            item.hydrated.chunk.chunk_id for item in candidates
        ),
    )
    assert provider.request is not None
    assert len(provider.request.candidates) == 24
    assert rare.hydrated.chunk.chunk_id in {
        key for key, _text in provider.request.candidates
    }
    assert len(outcome.retention_decisions) == 31
    assert any(
        reason == "RETENTION_QUOTA_EXHAUSTED"
        for _key, reason in outcome.retention_decisions
    )


def test_restored_output_scores_come_from_actual_rerank_response() -> None:
    candidates = tuple(
        make_ranked_chunk(number, "合成原文", must_keep=number == 31)
        for number in range(1, 32)
    )
    provider = _Reranker()
    outcome = CircuitAwareReranker(provider).rerank(
        "指定编号",
        candidates,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=3,
    )
    assert provider.request is not None
    sent = {key for key, _text in provider.request.candidates}
    assert len(sent) == 24
    assert candidates[-1].hydrated.chunk.chunk_id in sent
    assert candidates[-1].hydrated.chunk.chunk_id in {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }
    assert all(item.rerank_score is not None for item in outcome.candidates)
    assert all(
        item.hydrated.chunk.chunk_id in sent for item in outcome.candidates
    )


def test_safe_observation_has_explicit_total_and_no_source_body() -> None:
    candidate = make_ranked_chunk(1, "不应出现在SAFE的合成正文").model_copy(
        update={
            "retrieval_origins": (
                RetrievalOrigin(
                    unit_id="A1",
                    logical_channel="dense",
                    source_channel="dense:primary",
                    variant_id="original",
                    native_rank=1,
                ),
            ),
        }
    )
    observation = candidate_observation(
        (candidate,), selected_ids=frozenset(), drop_reason="INPUT_CAP"
    )
    assert observation["total"] == 1
    assert observation["truncated"] is False
    assert "不应出现在SAFE" not in repr(observation)
    assert "INPUT_CAP" in repr(observation)
    assert "retrieval_origins" not in candidate.model_dump(mode="json")


def test_same_channel_document_and_table_anchors_survive_crowding() -> None:
    """同通道前 25 条近似证据不能吞掉另一文档或另一张真实表。"""
    crowded = tuple(_table_candidate(number, 0) for number in range(1, 26))
    other_table = _table_candidate(26, 1)
    other_document = make_ranked_chunk(
        27, "另一文档的启动入口", document_number=9
    )
    selected, _, _ = _select_input(
        (*crowded, other_table, other_document),
        policy=RetrievalPolicy(),
        required_candidate_ids=frozenset(),
    )
    selected_ids = {item.hydrated.chunk.chunk_id for item in selected}
    assert len(selected) == 24
    assert other_document.hydrated.chunk.chunk_id in selected_ids
    assert other_table.hydrated.chunk.chunk_id in selected_ids


def test_many_tables_cannot_spend_another_documents_first_slot() -> None:
    """两个文档各在 16 条上限内，仍须保留第三文档的唯一入口。"""
    candidates = tuple(
        _table_candidate(number, number, document=2 if number <= 16 else 3)
        for number in range(1, 33)
    )
    rare = make_ranked_chunk(33, "第三份资料的启动入口", document_number=9)
    selected, _, _ = _select_input(
        (*candidates, rare),
        policy=RetrievalPolicy(),
        required_candidate_ids=frozenset(),
    )
    assert len(selected) == 24
    assert rare.hydrated.chunk.chunk_id in {
        item.hydrated.chunk.chunk_id for item in selected
    }


def _table_candidate(
    number: int, table: int, *, document: int = 2
) -> RankedChunk:
    candidate = make_ranked_chunk(
        number, "合成表格入口", role=ChunkRole.TABLE, document_number=document
    )
    chunk = candidate.hydrated.chunk
    span = chunk.source_spans[0]
    assert span.source_anchor is not None
    path = ("body", f"tbl:{table}", "tr:1", "tc:0", "p:1")
    span = span.model_copy(
        update={
            "structural_path": path,
            "source_anchor": span.source_anchor.model_copy(
                update={"structural_path": path}
            ),
        }
    )
    return candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(update={"source_spans": (span,)})
                }
            )
        }
    )


@pytest.mark.parametrize("field", ("revision_id", "document_version_id"))
def test_pre_fused_rejects_changed_raw_identity_before_hydration(
    field: str,
) -> None:
    """预融合不能让旧版本结构种子借用当前 chunk ID 获得恢复资格。"""
    valid = _hit(1, 1, channel="structural").model_copy(
        update={"match_type": "STRUCTURAL_TABLE_ROW"}
    )
    fused = reciprocal_rank_fusion(
        {"structural": (valid,)}, expected_revision_id=_REVISION
    )
    changed = valid.model_copy(
        update={
            field: f"{'irev' if field == 'revision_id' else 'dver'}_{'f' * 32}"
        }
    )
    with pytest.raises(IndexCorrupt):
        _capture_pre_fused_input({"structural": (changed,)}, fused)


@pytest.mark.parametrize("boundary", ("excluded", "acl"))
def test_pre_fused_does_not_restore_invisible_structural_seed(
    boundary: str,
) -> None:
    hit = _hit(1, 1, channel="structural").model_copy(
        update={"match_type": "STRUCTURAL_TABLE_ROW"}
    )
    fused = reciprocal_rank_fusion(
        {"structural": (hit,)}, expected_revision_id=_REVISION
    )
    service = _capture_pre_fused_input(
        {"structural": (hit,)}, fused, boundary=boundary
    )
    assert service._hydrator.hydrate.call_args.args[1] == ()
    assert not service._reranker.rerank.call_args.kwargs[
        "required_candidate_ids"
    ]


def _capture_pre_fused_input(
    channels: dict[str, tuple[ChannelHit, ...]],
    fused: tuple[FusedCandidate, ...],
    *,
    boundary: str = "none",
) -> RetrievalService:
    service = object.__new__(RetrievalService)
    service._policy = RetrievalPolicy()
    service._hydrator = Mock()
    service._hydrator.hydrate.return_value = ()
    service._trace = Mock()
    service._egress = EgressPolicy()
    service._reranker = Mock()
    service._reranker.rerank.side_effect = RuntimeError("captured-rerank")
    snapshot = SimpleNamespace(
        excluded_document_ids=(fused[0].document_id,)
        if boundary == "excluded"
        else (),
        revision=SimpleNamespace(index_revision_id=_REVISION),
    )
    analysis = _unit("ROOT", (), atom_count=1).unit.analysis
    plan = QueryPlanner().plan(
        analysis,
        (
            QueryVariant(
                text="启动需要什么材料",
                kind="original",
                identity="sha256:" + "1" * 64,
            ),
        ),
        service._policy,
    )
    request = SearchRequest(scope=_SCOPE, text="启动需要什么材料")
    if boundary == "acl":
        request = request.model_copy(
            update={"access_filters": (("allowed_document_ids", ()),)}
        )
    with pytest.raises(RuntimeError, match="captured-rerank"):
        service._rank_and_select(
            request=request,
            snapshot=snapshot,
            analysis=analysis,
            plan=plan,
            channel_hits=channels,
            selected_slot=None,
            trace_id=f"trace_{'9' * 32}",
            provider_calls=[],
            degraded=[],
            stage_timings=[],
            retrieval_phase="original",
            pre_fused=fused,
        )
    return service
