"""冻结的通用属性对照；全部是 unit_synthetic，禁止作为 Live 证据。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    ConfidenceStatus,
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
    RetrievalPolicy,
    SearchRequest,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_PAIRS = (
    ("冷却器采购价格是多少？", "冷却器数量为12台，温度为8℃。", False),
    ("冷却器采购价格是多少？", "冷却器采购价格为1200元。", True),
    (
        "冷却器采购价格是多少？",
        "冷却器数量为12台；加热器采购价格为1200元。",
        False,
    ),
    ("驱动器售价多少？", "驱动器售价未提供。", False),
    ("驱动器售价多少？", "驱动器售价为35美元。", True),
    ("驱动器采购价格多少？", "驱动器售价为35美元。", False),
    ("阅览厅建筑面积是多少？", "阅览厅长度为20米，位于3楼。", False),
    ("阅览厅建筑面积是多少？", "阅览厅建筑面积为240平方米。", True),
    (
        "阅览厅建筑面积是多少？",
        "阅览厅位于3楼；会议厅建筑面积为240平方米。",
        False,
    ),
    ("实验楼面积多大？", "实验楼面积未提供。", False),
    ("实验楼面积多大？", "实验楼面积为125.5m²。", True),
    ("实验楼面积多大？", "实验楼占地面积未知，长度为125.5m。", False),
    ("值班员联系电话是多少？", "接待申请由值班员受理。", False),
    ("值班员联系电话是多少？", "值班员联系电话为010-87654321。", True),
    (
        "值班员联系电话是多少？",
        "值班员负责接待；保安联系电话为010-87654321。",
        False,
    ),
    ("调度员分机号是多少？", "调度员的设备编号为87654321。", False),
    ("调度员分机号是多少？", "调度员分机号为8236。", True),
    ("调度员分机号是多少？", "调度员分机号尚未确定。", False),
    ("维修申请应找谁？", "维修申请由设备管理员受理。", True),
    ("谁负责复核报销申请？", "报销申请由财务审核员复核。", True),
    ("办理地址纠正应找谁？", "地址更正申请由服务专员受理。", True),
    ("退货需要谁签字？", "退货需要销售主管签字。", True),
    ("维修申请应找谁？", "维修申请已收到，受理人员尚未确定。", False),
    ("维修申请应找谁？", "维修申请已收到；采购申请由采购经理受理。", False),
)


@pytest.mark.parametrize(("query", "quote", "answerable"), _PAIRS)
def test_frozen_attribute_pairs(
    query: str, quote: str, answerable: bool
) -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(
            scope=KnowledgeBaseScope(
                project_id=f"prj_{'1' * 32}",
                knowledge_base_id=f"kb_{'2' * 32}",
            ),
            text=query,
        )
    )
    candidate = make_ranked_chunk(1, quote)
    policy = RetrievalPolicy()
    evidence = EvidenceAssembler().assemble(
        (candidate,),
        policy,
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
        ),
    )
    decision = ConfidenceEvaluator().evaluate(
        analysis,
        QueryKind.SIMPLE_FACT,
        (candidate,),
        evidence,
        (),
        policy=policy,
    )
    assert (decision.status is ConfidenceStatus.ANSWERABLE) is answerable
    if answerable:
        assert evidence and evidence[0].source_spans
