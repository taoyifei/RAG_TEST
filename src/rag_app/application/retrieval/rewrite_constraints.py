"""改写只改变问句表达，业务对象及硬限定必须由原问题逐项支持。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.models import (
    QueryAnalysis,
    RequestedAnswerType,
    SearchRequest,
)

# 问句语法可改写，词项来自每次请求，不维护任何业务问题白名单。
_DUTY_QUESTION = re.compile(
    r"具体干什么|具体做什么|干些什么|做些什么|负责哪些工作|承担哪些工作|"
    r"干什么|干啥|做什么|工作内容|职能|职责"
)
_QUESTION_SYNTAX = re.compile(
    r"我想知道|我想了解|请告诉我|告诉我|请帮我|请列举|请列出|请介绍|"
    r"请说明|请解释|请问|请|具体|详细|到底|说白了|怎么说|分别|"
    r"是什么|是啥|是哪些|有哪些|有哪几种|哪几种|哪一些|哪些|哪种|什么|"
    r"多少|如何|怎么|咋|需要承担|主要负责(?!人)|负责(?!人)|承担|"
    r"关于|对于|方面|以及|或者|"
    r"\b(?:what|which|does|do|is|are|the|a|an|please|of|for)\b",
    flags=re.IGNORECASE,
)
_LEADING_TOPIC_SYNTAX = re.compile(r"^(?:对|把|由)\s*")
_TRAILING_TOPIC_PARTICLES = re.compile(r"[呢吗呀啊吧嘛？?。！!，,\s]+$")
_RELATION_POSSESSIVE = re.compile(
    r"的(?=工作模式|协作方式|运行方式|工作方式|模式|类型|种类|类别|"
    r"分类|职责|步骤|流程|负责人|责任人)"
)
_TOPIC_FRAME = re.compile(r"在(?P<body>[^，,。；;！!？?\n]{1,80}?)方面")
_NUMBER = re.compile(
    r"[+-]?\d+(?:[.,:/-]\d+)*(?:\s*(?:%|％|亿元|万元|元|"
    r"毫秒|分钟|小时|秒|天|周|个月|年|月|日|毫米|厘米|千米|米|"
    r"公斤|千克|毫克|克|吨|毫升|升|次|个|台|件|人|℃|"
    r"[A-Za-zμµΩ°]+(?:/[A-Za-z]+)?))?"
)
_NEGATION = re.compile(
    r"不得|禁止|严禁|不能|不可|不允许|不准|无需|不必|不需要|"
    r"尚未|没有|并非|不是|未|无|不|\b(?:not|without|never|no)\b",
    flags=re.IGNORECASE,
)
_QUALIFIER = re.compile(
    r"仅限|仅仅|只限|只有|至少|至多|最多|最少|不超过|不低于|不少于|"
    r"不高于|超过|低于|高于|大于|小于|之前|之后|以前|以后|以上|以下|"
    r"以内|以外|除外|期间|当前|目前|历史|全部|所有|任何|任意|每个|每一|"
    r"仅|只|除|必须|应当|可以|可能|\b(?:only|all|any|every|before|after)\b",
    flags=re.IGNORECASE,
)
_TEXT = re.compile(r"[A-Za-z0-9_\-\u3400-\u9fff]+")
_SUBJECT = re.compile(
    r"([^，,。；;！!？?\n]{1,80}?)"
    r"(?:的)?(?:职责|负责(?!人)|承担|审批|批准|审核|核准)"
)


def rewrite_constraint_reason(request: SearchRequest, text: str) -> str | None:
    """核对原问题的硬限定、主体和业务词项，允许语法变化及合法调序。

    Args:
        request: 原始范围固定的查询，正文不被原地修改。
        text: 模型返回的唯一候选改写。

    Returns:
        拒绝原因；满足约束时返回 None。指代信息不足时保守保留原问题。

    """
    analyzer = QueryAnalyzer()
    original = analyzer.analyze(request)
    rewritten = analyzer.analyze(request.model_copy(update={"text": text}))
    fields = (
        "identifiers",
        "numbers",
        "units",
        "date_version_signals",
        "negation_signals",
        "quoted_phrases",
    )
    before = _normalize(request.text)
    after = _normalize(text)
    hard_changed = any(
        Counter(getattr(original, field)) != Counter(getattr(rewritten, field))
        for field in fields
    ) or any(
        _atoms(pattern, before) != _atoms(pattern, after)
        for pattern in (_NUMBER, _NEGATION, _QUALIFIER)
    )
    # 字面信号允许调序，不能新增、删除或替换。
    if hard_changed:
        return "REWRITE_CONSTRAINT_CHANGED"
    semantic_reason = _semantic_change_reason(original, rewritten)
    if semantic_reason is not None:
        return semantic_reason
    before_terms = _topics(before)
    after_terms = _topics(after)
    if not before_terms or not after_terms:
        return None if before == after else "REWRITE_SCOPE_CHANGED"
    if not _same_topics(before_terms, after_terms):
        return "REWRITE_SCOPE_CHANGED"
    before_subject = _subject_topics(before)
    after_subject = _subject_topics(after)
    if (
        before_subject
        and after_subject
        and not all(_covered(term, after_subject) for term in before_subject)
    ):
        return "REWRITE_SCOPE_CHANGED"
    return None


def _semantic_change_reason(
    original: QueryAnalysis, rewritten: QueryAnalysis
) -> str | None:
    """对共享语义可确认的问法做精确对象、关系与形状比较。"""
    before = original.semantics
    after = rewritten.semantics
    descriptive = {
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.DUTIES,
        RequestedAnswerType.PROCEDURE,
    }
    if before.answer_type in descriptive or after.answer_type in descriptive:
        if before.answer_type is not after.answer_type:
            return "REWRITE_CONSTRAINT_CHANGED"
        if (before.expected_count, before.ordinal) != (
            after.expected_count,
            after.ordinal,
        ):
            return "REWRITE_CONSTRAINT_CHANGED"
        if (
            before.answer_type is not RequestedAnswerType.DUTIES
            and before.target
            and after.target
            and before.target != after.target
        ):
            return "REWRITE_SCOPE_CHANGED"
        if (
            before.relation
            and after.relation
            and before.relation != after.relation
        ):
            return "REWRITE_SCOPE_CHANGED"
    return None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _atoms(pattern: re.Pattern[str], text: str) -> Counter[str]:
    return Counter(re.sub(r"\s+", "", value) for value in pattern.findall(text))


def _topics(text: str) -> tuple[str, ...]:
    text = _DUTY_QUESTION.sub("职责", text)
    # 保留职责这一关系词；先处理其他口语，再去除问句语法。
    text = re.sub(r"怎么说|说白了|咋", "如何", text)
    text = _TOPIC_FRAME.sub(lambda match: match["body"], text)
    text = _LEADING_TOPIC_SYNTAX.sub("", text)
    text = _RELATION_POSSESSIVE.sub("", text)
    text = _TRAILING_TOPIC_PARTICLES.sub("", text)
    for pattern in (_NUMBER, _NEGATION, _QUALIFIER, _QUESTION_SYNTAX):
        text = pattern.sub(" ", text)
    return tuple(_TEXT.findall(text))


def _same_topics(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    return all(_covered(term, after) for term in before) and all(
        _covered(term, before) for term in after
    )


def _covered(term: str, available: tuple[str, ...]) -> bool:
    """按原始词项精确覆盖，允许插入语法分隔和改变并列词项顺序。"""
    compact = "".join(available)
    if term in compact:
        return True
    reachable = {0}
    for start in range(len(term)):
        if start in reachable:
            reachable.update(
                start + len(token)
                for token in available
                if term.startswith(token, start)
            )
    return len(term) in reachable


def _subject_topics(text: str) -> tuple[str, ...]:
    canonical = _DUTY_QUESTION.sub("职责", text)
    match = _SUBJECT.search(canonical)
    return () if match is None else _topics(match[1])
