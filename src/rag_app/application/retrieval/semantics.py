"""中文自然问法的共享类型化语义，不包含业务实体白名单。"""

from __future__ import annotations

import re
import unicodedata

from rag_app.core.models import QuerySemantics, RequestedAnswerType

_NUMERAL = r"[零一二三四五六七八九十百两\d]+"
_RELATION = re.compile(
    r"工作模式|协作方式|运行方式|工作方式|模式|类型|种类|类别|分类|职责|步骤|流程"
)
_DUTY = re.compile(
    r"(?:(?:具体|主要)(?:地)?)?"
    r"(?:职责(?:是什么|是啥|有哪些|包括什么)?|"
    r"负责(?:什么|啥|哪些)(?:工作|事项|内容)?|"
    r"(?:需要)?承担(?:什么|哪些)(?:职责|工作|事项|内容)?|"
    r"干(?:什么|啥|些什么)|做(?:什么|啥|些什么))"
    r"(?:工作|事项|内容)?[呢吗呀啊？?\s]*$"
)
_CONDITION_OR_ORDER = re.compile(
    r"如果|一旦|若|当.+时|之前|之后|以前|以后|期间|过程中|"
    r"(?:先|再|然后|随后|接着|前|后|时)(?:应|要|需|应该|需要)?$"
)
_ORDINAL = re.compile(rf"第(?P<number>{_NUMERAL})(?:种|类|项|步|条)")
_COUNT_QUESTION = re.compile(
    r"(?:有)?多少(?:种|个)?(?:步骤|步|项)?|(?<!哪)几种|几(?:个)?步骤"
)
_EXPECTED_COUNT = re.compile(
    rf"(?<!第)(?P<number>{_NUMERAL})(?:种|类|项|步)(?![类型])"
)
_ENUMERATION_QUESTION = re.compile(
    r"是什么|是啥|(?:都)?有啥|(?:都)?有哪些|有哪(?:几|"
    + _NUMERAL
    + r")种|是哪(?:几|"
    + _NUMERAL
    + r")种|哪(?:几|"
    + _NUMERAL
    + r")种|分别(?:是|指)?(?:什么|啥)|"
    r"(?:具体)?(?:是)?怎么(?:说|表述)(?:的)?|请列举|请列出|列出来|列一下"
)
_PROCEDURE_QUESTION = re.compile(
    r"是什么|是啥|(?:都)?有哪些|有哪(?:几|"
    + _NUMERAL
    + r")步|哪(?:几|"
    + _NUMERAL
    + r")步|分别(?:是|指)?(?:什么|啥)|请列举|请列出|列出来|列一下|"
    r"如何|怎么|怎样"
)
_LEADING_REQUEST = re.compile(
    r"^(?:请问|请告诉我|告诉我|我想知道|请帮我|请介绍|请说明|"
    r"请解释|请列举|请列出|请)"
)
_TRAILING_TARGET_SYNTAX = re.compile(
    rf"(?:的)?(?:第{_NUMERAL}(?:种|类|项|步|条)|"
    rf"{_NUMERAL}(?:种|类)|(?:是)?哪(?:几|{_NUMERAL})(?:种|类)|"
    r"有多少种|(?:都)?有哪些|(?:都)?有啥|采用的|现有的|主要的|核心的|的)$"
)
_TRAILING_PARTICLES = re.compile(
    r"(?:(?:嘛|吧)[，,]|[呢吗呀啊？?。！!，,\s])+$"
)
_SOURCE_QUALIFIED_DUTY = re.compile(
    r"^(?P<source>.+?(?:规范|文档|制度|手册))(?:里|中)(?P<target>.+)$"
)


def parse_query_semantics(query: str) -> QuerySemantics:
    """从确认的问句位置提取对象、关系和答案形状。

    Args:
        query: NFKC 前后均可接受的单个用户问题。

    Returns:
        规则命中时返回类型化语义；未覆盖表达保持 unknown。

    """
    normalized = unicodedata.normalize("NFKC", query).strip()
    duty = _DUTY.search(normalized)
    if duty is not None and not _CONDITION_OR_ORDER.search(
        normalized[: duty.start()]
    ):
        prefix = normalized[: duty.start()]
        target = _clean_target(prefix, duty=True)
        if target:
            return QuerySemantics(
                target=target,
                source_qualifier=_source_qualifier(prefix),
                relation="职责",
                answer_type=RequestedAnswerType.DUTIES,
                source="RULE",
                reason_codes=("DUTY_QUESTION_SYNTAX",),
            )

    relation_match = _RELATION.search(normalized)
    if relation_match is None:
        return _fallback_semantics(normalized)

    prefix = normalized[: relation_match.start()]
    suffix = normalized[relation_match.end() :]
    relation = relation_match.group(0)
    ordinal_match = _ORDINAL.search(prefix + suffix)
    count_question = _COUNT_QUESTION.search(prefix + suffix)
    expected_match = _EXPECTED_COUNT.search(prefix + suffix)
    expected_count = (
        _number_value(expected_match["number"])
        if expected_match is not None
        else None
    )
    ordinal = (
        _number_value(ordinal_match["number"])
        if ordinal_match is not None
        else None
    )
    if ordinal is not None:
        answer_type = RequestedAnswerType.ORDINAL_ITEM
    elif count_question is not None:
        answer_type = RequestedAnswerType.COUNT
        expected_count = None
    elif relation in {"步骤", "流程"} and _PROCEDURE_QUESTION.search(
        normalized
    ):
        answer_type = RequestedAnswerType.PROCEDURE
    elif _ENUMERATION_QUESTION.search(normalized):
        answer_type = RequestedAnswerType.ENUMERATION
    elif relation == "职责":
        answer_type = RequestedAnswerType.DUTIES
    else:
        answer_type = RequestedAnswerType.UNKNOWN
    if answer_type is RequestedAnswerType.UNKNOWN:
        return QuerySemantics(
            relation=relation,
            answer_type=answer_type,
            source="ORIGINAL_FALLBACK",
            reason_codes=("QUERY_SEMANTICS_UNKNOWN",),
        )
    target = _clean_target(
        prefix, duty=answer_type is RequestedAnswerType.DUTIES
    )
    if not target:
        return QuerySemantics(
            relation=relation,
            answer_type=RequestedAnswerType.UNKNOWN,
            source="ORIGINAL_FALLBACK",
            reason_codes=("QUERY_TARGET_UNKNOWN",),
        )
    return QuerySemantics(
        target=target,
        relation=relation,
        answer_type=answer_type,
        expected_count=expected_count,
        ordinal=ordinal,
        source="RULE",
        reason_codes=("SHARED_QUERY_SEMANTICS_V1",),
    )


def _fallback_semantics(query: str) -> QuerySemantics:
    answer_type = (
        RequestedAnswerType.FACT
        if re.search(
            r"谁|什么|啥|多少|如何|怎么|怎样|哪|何时|是否|能否|几|[?？]",
            query,
        )
        else RequestedAnswerType.UNKNOWN
    )
    return QuerySemantics(
        answer_type=answer_type,
        source="ORIGINAL_FALLBACK",
        reason_codes=("QUERY_SEMANTICS_UNKNOWN",),
    )


def _clean_target(value: str, *, duty: bool) -> str:
    """只移除对象边界上的问句语法，绝不全字符串删词。"""
    target = _TRAILING_PARTICLES.sub("", value.strip())
    target = _LEADING_REQUEST.sub("", target).strip()
    if target.startswith("把"):
        target = target[1:].strip()
    previous = None
    while target and target != previous:
        previous = target
        target = _TRAILING_TARGET_SYNTAX.sub("", target).strip()
        target = _TRAILING_PARTICLES.sub("", target).strip()
    if duty:
        target = re.split(r"(?:规范|文档|制度|手册)(?:里|中)", target)[-1]
        target = re.sub(r"(?:的)?(?:主要|核心|具体)$", "", target).strip()
    return target


def _source_qualifier(value: str) -> str | None:
    """提取“某规范中/里”的动态来源限定，不维护文档名白名单。"""
    candidate = _LEADING_REQUEST.sub("", value.strip()).strip()
    match = _SOURCE_QUALIFIED_DUTY.fullmatch(candidate)
    if match is None:
        return None
    qualifier = match["source"].strip(" 的")
    return qualifier or None


def _number_value(value: str) -> int | None:
    """解析问句中小规模阿拉伯数字或常用中文序数。"""
    if value.isdigit():
        number = int(value)
        return number if number > 0 else None
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "百" in value:
        head, _, tail = value.partition("百")
        return digits.get(head or "一", 1) * 100 + (_number_value(tail) or 0)
    if "十" in value:
        head, _, tail = value.partition("十")
        return digits.get(head or "一", 1) * 10 + digits.get(tail, 0)
    return digits.get(value)


__all__ = ["parse_query_semantics"]
