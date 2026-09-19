"""受控 private replay 的原始模型草稿取证合同。"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from rag_app.core.models import (
    AnswerDraft,
    ClaimSupport,
    EvidenceItem,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
)
from rag_app.core.models.retrieval import NaturalClaim
from rag_app.core.ports.generator import GenerationRequest
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.private_replay import PrivateReplayDraftRecorder
from tests.application.retrieval.helpers import make_ranked_chunk

_ACK = "private-replay-v1"


def _request_and_draft() -> tuple[GenerationRequest, AnswerDraft]:
    ranked = make_ranked_chunk(1, "合成来源原句。")
    chunk = ranked.hydrated.chunk
    evidence = EvidenceItem(
        evidence_id="S1",
        chunk_id=chunk.chunk_id,
        citation_text="合成来源原句。",
        source_label="合成来源",
        source_spans=chunk.source_spans,
        document_id="doc_" + "1" * 32,
        document_version_id="dver_" + "2" * 32,
    )
    plan = QueryPlan(
        plan_id="sha256:" + "3" * 64,
        standalone_query="合成问题",
        original_query="合成问题",
        resolved_root_query="合成问题",
        context_digest="sha256:" + "4" * 64,
        intent="FACT",
        effort="DIRECT",
        atoms=(
            QueryAtom(
                atom_id="A1",
                target="合成对象",
                relation="合成关系",
                answer_shape=AtomAnswerShape.FACT,
            ),
        ),
        planner_reason_code="SYNTHETIC",
    )
    matrix = AtomSupportMatrix(
        atoms=(
            AtomSupport(
                atom_id="A1",
                status=AtomStatus.PARTIAL,
                supporting_support_ids=("S1",),
            ),
        )
    )
    request = GenerationRequest(
        query="合成问题",
        evidence=(evidence,),
        citation_protocol="support-id-v3-quoted-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
        per_atom_candidate_support_ids=(("A1", ("S1",)),),
    )
    draft = AnswerDraft(
        text="合成模型草稿",
        cited_evidence_ids=("S1",),
        natural_claims=(
            NaturalClaim(
                atom_id="A1",
                text="合成模型事实。",
                supports=(
                    ClaimSupport(
                        support_id="S1",
                        quote="合成来源原句。",
                    ),
                ),
            ),
        ),
        generation_mode="natural",
    )
    return request, draft


def test_private_replay_capture_preserves_raw_draft_and_source_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受控目录保存模型实际 Claim、Quote、允许集合和来源跨度。"""
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    private_dir.chmod(0o700)
    monkeypatch.setenv("RAG_PRIVATE_REPLAY_CAPTURE_DIR", str(private_dir))
    monkeypatch.setenv("RAG_PRIVATE_REPLAY_CAPTURE_ACK", _ACK)
    monkeypatch.setenv("RAG_PRIVATE_REPLAY_CAPTURE_LIMIT", "4")
    recorder = PrivateReplayDraftRecorder.from_environment()
    assert recorder is not None
    request, draft = _request_and_draft()

    recorder.record(request, draft)

    output = private_dir / "raw-generation-drafts.ndjson"
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["request"]["query"] == "合成问题"
    assert row["request"]["per_atom_candidate_support_ids"] == [
        ["A1", ["S1"]]
    ]
    assert row["request"]["evidence"][0]["source_spans"]
    claim = row["draft"]["natural_claims"][0]
    assert claim["text"] == "合成模型事实。"
    assert claim["supports"] == [
        {"support_id": "S1", "quote": "合成来源原句。"}
    ]
    assert output.stat().st_mode & 0o077 == 0


def test_private_replay_capture_is_disabled_without_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通产品启动不会创建或记录任何私有草稿。"""
    monkeypatch.delenv("RAG_PRIVATE_REPLAY_CAPTURE_DIR", raising=False)
    monkeypatch.delenv("RAG_PRIVATE_REPLAY_CAPTURE_ACK", raising=False)
    monkeypatch.delenv("RAG_PRIVATE_REPLAY_CAPTURE_LIMIT", raising=False)

    assert PrivateReplayDraftRecorder.from_environment() is None


def test_private_replay_capture_rejects_group_readable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """私有目录向组或其他用户开放时在产品启动阶段拒绝。"""
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o750)
    private_dir.chmod(0o750)
    monkeypatch.setenv("RAG_PRIVATE_REPLAY_CAPTURE_DIR", str(private_dir))
    monkeypatch.setenv("RAG_PRIVATE_REPLAY_CAPTURE_ACK", _ACK)
    monkeypatch.setenv("RAG_PRIVATE_REPLAY_CAPTURE_LIMIT", "4")

    with pytest.raises(
        ValueError, match="PRIVATE_REPLAY_DIRECTORY_PERMISSIONS"
    ):
        PrivateReplayDraftRecorder.from_environment()


@pytest.mark.parametrize("stream", [False, True])
def test_product_grounded_model_records_actual_returned_draft(
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步和流式生成都在业务校验前交给同一个私有记录器。"""
    request, draft = _request_and_draft()
    recorder = Mock(spec=PrivateReplayDraftRecorder)
    model = object.__new__(ProductGroundedModel)
    model._private_replay_recorder = recorder
    monkeypatch.setattr(model, "_source_hashes", Mock(return_value=()))
    monkeypatch.setattr(model, "_scope", Mock(return_value=nullcontext()))
    monkeypatch.setattr(
        model,
        "_call_with_rotation",
        Mock(return_value=(draft, ())),
    )

    if stream:
        result = model.generate_stream(
            request,
            on_claim=Mock(),
            cancellation=Mock(),
        )
    else:
        result = model.generate(request)

    recorder.record.assert_called_once_with(request, result)
