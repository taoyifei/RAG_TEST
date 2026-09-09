"""问题改写保留动态业务词项，语法调整不依赖预置问题。"""

import pytest

from rag_app.application.retrieval.rewrite_constraints import (
    rewrite_constraint_reason,
)
from rag_app.core.models import KnowledgeBaseScope, SearchRequest


def _request(text: str) -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id="prj_" + "a" * 32, knowledge_base_id="kb_" + "b" * 32
        ),
        text=text,
    )


@pytest.mark.parametrize(
    "before,after",
    [
        (
            "甲部门负责设备维护，具体干什么？",
            "乙部门负责设备维护，具体干什么？",
        ),
        (
            "甲部门负责设备维护，具体干什么？",
            "甲部门负责设备采购，具体干什么？",
        ),
        (
            "甲部门负责设备维护，具体干什么？",
            "甲部门和乙部门负责设备维护，具体干什么？",
        ),
        ("张三负责文件归档，具体干什么？", "李四负责文件归档，具体干什么？"),
        ("ZX-471 控制模块具体干什么？", "ZX-472 控制模块具体干什么？"),
        (
            "甲部门审核乙部门资料，具体干什么？",
            "乙部门审核甲部门资料，具体干什么？",
        ),
    ],
)
def test_changed_subject_topic_or_added_object_is_rejected(
    before: str, after: str
) -> None:
    assert rewrite_constraint_reason(_request(before), after) is not None


@pytest.mark.parametrize(
    "before,after",
    [
        ("维护周期14天，具体干什么？", "维护周期4天，具体干什么？"),
        ("维护延时14毫秒，具体干什么？", "维护延时14分钟，具体干什么？"),
        (
            "2026年9月1日之后甲部门具体干什么？",
            "2027年9月1日之后甲部门具体干什么？",
        ),
        ("甲部门未批准申请，具体干什么？", "甲部门批准申请，具体干什么？"),
        (
            "甲部门不得自行维护，具体干什么？",
            "甲部门无需自行维护，具体干什么？",
        ),
        (
            "仅甲部门负责设备维护，具体干什么？",
            "甲部门负责设备维护，具体干什么？",
        ),
        (
            "甲部门最多负责14台设备，具体干什么？",
            "甲部门至少负责14台设备，具体干什么？",
        ),
        ("甲部门在验收之前具体干什么？", "甲部门在验收之后具体干什么？"),
    ],
)
def test_numbers_dates_negation_and_scope_cannot_change(
    before: str, after: str
) -> None:
    assert (
        rewrite_constraint_reason(_request(before), after)
        == "REWRITE_CONSTRAINT_CHANGED"
    )


@pytest.mark.parametrize(
    "before,after",
    [
        (
            "甲部门负责设备维护，具体干什么？",
            "设备维护方面，甲部门的职责有哪些？",
        ),
        ("张三负责记录归档，干啥？", "张三在记录归档方面的职责是什么？"),
        ("ZX-471 控制模块具体干什么？", "ZX-471 控制模块的职责是什么？"),
        (
            "甲部门负责设备维护，具体干什么？",
            "甲部门负责设备维护，具体干什么？",
        ),
    ],
)
def test_colloquial_grammar_and_topic_reordering_are_allowed(
    before: str, after: str
) -> None:
    request = _request(before)
    assert rewrite_constraint_reason(request, after) is None
    assert request.text == before


def test_pronoun_can_resolve_only_to_scoped_conversation_object() -> None:
    """指代可使用会话对象，但不能借上下文加入新事实或其它对象。"""
    request = _request("它的维护周期是多少？").model_copy(
        update={
            "conversation_context": (
                "上一问：设备 MX-41 是什么？\n"
                "已验证事实：MX-41 是公开合成设备。",
            )
        }
    )

    assert (
        rewrite_constraint_reason(request, "MX-41 的维护周期是多少？") is None
    )
    assert (
        rewrite_constraint_reason(request, "MX-42 的维护周期是多少？")
        == "REWRITE_CONSTRAINT_CHANGED"
    )
    assert (
        rewrite_constraint_reason(request, "MX-41 的维护周期是 14 天吗？")
        == "REWRITE_CONSTRAINT_CHANGED"
    )
    assert (
        rewrite_constraint_reason(
            _request("它的维护周期是多少？"),
            "MX-41 的维护周期是多少？",
        )
        == "REWRITE_CONSTRAINT_CHANGED"
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("开发中心的工作模式是啥", "开发中心的工作模式是什么"),
        ("美的中心的工作模式是啥", "美的中心的工作模式是什么"),
        (
            "研发和质量组的工作模式是啥",
            "研发和质量组的工作模式是什么",
        ),
        (
            "“啥都有”研究组的工作模式是啥",
            "“啥都有”研究组的工作模式是什么",
        ),
    ),
)
def test_descriptive_rewrite_preserves_entity_names(
    before: str, after: str
) -> None:
    assert rewrite_constraint_reason(_request(before), after) is None


def test_duty_rewrite_preserves_dynamic_source_qualifier() -> None:
    before = "蓝熊规范中项目经理具体负责哪些工作"

    assert (
        rewrite_constraint_reason(
            _request(before), "蓝熊规范中项目经理的职责是什么"
        )
        is None
    )
    assert (
        rewrite_constraint_reason(
            _request(before), "白鹭规范中项目经理的职责是什么"
        )
        == "REWRITE_SCOPE_CHANGED"
    )
    assert (
        rewrite_constraint_reason(_request(before), "项目经理的职责是什么")
        == "REWRITE_SCOPE_CHANGED"
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("开发中心在哪里？", "开发心在哪里？"),
        ("研发和质量组在哪里？", "研发质量组在哪里？"),
        ("美的中心在哪里？", "美中心在哪里？"),
        ("啥都有研究组在哪里？", "都有研究组在哪里？"),
        ("里程碑小组在哪里？", "程碑小组在哪里？"),
    ),
)
def test_fallback_rewrite_cannot_delete_entity_function_characters(
    before: str, after: str
) -> None:
    """未知关系也按原词比较，不能靠全局删虚词掩盖实体漂移。"""
    assert (
        rewrite_constraint_reason(_request(before), after)
        == "REWRITE_SCOPE_CHANGED"
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (
            "蓝鹊小组的三种工作模式是啥",
            "蓝鹊小组的四种工作模式是什么",
        ),
        (
            "蓝鹊小组的第三种工作模式是什么",
            "蓝鹊小组的第二种工作模式是什么",
        ),
        (
            "蓝鹊小组有多少种工作模式",
            "蓝鹊小组有哪些工作模式",
        ),
    ),
)
def test_rewrite_cannot_change_count_or_answer_shape(
    before: str, after: str
) -> None:
    assert rewrite_constraint_reason(_request(before), after) is not None
