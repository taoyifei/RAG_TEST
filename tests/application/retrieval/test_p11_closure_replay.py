"""原公开 30 问双路历史候选复放，不能冒充新 Live。"""

from __future__ import annotations

import hashlib
import json
import socket
from typing import Any

import pytest

from tests.support.p11_closure_replay import INPUT, ROOT, no_network, run_replay


@pytest.fixture(scope="module")
def replay() -> dict[str, Any]:
    """只读一次固定输入并封网运行全部真实排名。"""
    return run_replay()


@pytest.mark.parametrize("lane", ("primary", "standby"))
@pytest.mark.parametrize("number", range(30))
def test_original_observation_regression(
    replay: dict[str, Any], lane: str, number: int
) -> None:
    """逐条检查原标签及真实来源，无需 Provider 或 Qdrant。"""
    details = [item for item in replay["details"] if item["lane"] == lane]
    assert len(details) == 30
    detail = details[number]
    assert detail["correct"], (
        detail["case_id"],
        detail["status"],
        detail["source_count"],
        detail["generator_text"],
    )


@pytest.mark.parametrize("lane", ("primary", "standby"))
def test_original_quality_gates_in_offline_replay(
    replay: dict[str, Any], lane: str
) -> None:
    """原指标算法和门禁原值仍完整使用，离线状态不能成为 Live PASS。"""
    report = replay["report"]
    assert report["status"] == "NOT_RUN"
    assert report["gates"][lane]["passed"], [
        item for item in report["gates"][lane]["outcomes"] if not item["passed"]
    ]


def test_replay_keeps_missing_scores_unknown_and_http_zero(
    replay: dict[str, Any],
) -> None:
    """缺失分数保留为空；重建排名不是伪造 Provider 回包。"""
    assert replay["provider_http"] == 0
    assert "rerank_scores" in replay["missing_fields"]
    assert all(
        item["rerank_score"] is None
        for detail in replay["details"]
        for item in detail["ranked"]
    )
    assert all(
        observation["provider_call_count"] == 0
        for lane in replay["observations"].values()
        for observation in lane
    )


def test_replay_network_guard_blocks_socket_calls() -> None:
    """验证封网确实生效，而非只记录计数零。"""
    with (
        no_network(),
        socket.socket() as connection,
        pytest.raises(AssertionError, match="REPLAY_NETWORK_FORBIDDEN"),
    ):
        connection.connect(("127.0.0.1", 1))


def test_related_preview_keeps_all_sixty_original_metrics(
    replay: dict[str, Any],
) -> None:
    """原正反例和原指标全量开关对照；相关内容不进入有效引用分子。"""
    shown = run_replay(include_related_content=True)
    assert shown["observations"] == replay["observations"]
    assert shown["report"]["gates"] == replay["report"]["gates"]
    assert shown["provider_http"] == replay["provider_http"] == 0
    assert any(row["related_contents"] for row in shown["details"])
    for plain, row in zip(replay["details"], shown["details"], strict=True):
        for key in (
            "status",
            "confidence",
            "evidence",
            "generator_text",
            "source_count",
        ):
            assert plain[key] == row[key]
        if row["status"] == "ANSWERABLE":
            assert not row["related_contents"]
        else:
            assert row["generator_text"] is None
            assert all(
                not item["is_answer_evidence"]
                for item in row["related_contents"]
            )


def test_original_contract_hashes_are_frozen() -> None:
    """原题目、标签、来源范围、语料和阈值没有为通过而修改。"""
    fixture = json.loads(INPUT.read_text("utf-8"))
    for relative, expected in fixture["frozen_contract_hashes"].items():
        assert (
            hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            == expected
        )


def test_frozen_canonical_quotes_are_public_synthetic() -> None:
    """Fixture 每个可引用片段都来自已批准的公开合成语料。"""
    fixture = json.loads(INPUT.read_text("utf-8"))
    corpus = json.loads(
        (ROOT / "evaluation/datasets/p11-pilot/corpus.json").read_text("utf-8")
    )
    public_text = {
        text for document in corpus.values() for text in document["paragraphs"]
    } | {
        cell
        for document in corpus.values()
        for table in document["tables"]
        for row in table
        for cell in row
    }
    for chunk in fixture["inventory"]["chunks"]:
        for span in chunk["source_spans"]:
            if span["is_citable"]:
                quote = chunk["citation_text"][
                    span["chunk_start_char"] : span["chunk_end_char"]
                ]
                assert quote in public_text
