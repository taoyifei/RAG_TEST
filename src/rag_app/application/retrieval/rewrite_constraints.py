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
_CONTEXT_REFERENCE = re.compile(
    r"这个|那个|它|其中|上述|前者|后者|刚才提到的|前面提到的"
)
_INTERPRETATION_FILLER = re.compile(
    r"干嘛|怎么回事|咋回事|什么情况|啥情况|这(?:一)?块|这(?:件)?事|"
    r"这方面|情况|回事|的|是|为"
)


def rewrite_constraint_reason(request: SearchRequest, text: str) -> str | None:
    """核对原问题的硬限定、主体和业务词项，允许语法变化及合法调序。

    Args:
        request: 原始范围固定的查询，正文不被原地修改。
        text: 模型返回的唯一候选改写。

    Returns:
        拒绝原因；满足约束时返回 None。指代信息不足时保守保留原问题。

    """
    return _surface_constraint_reason(
        request, text, validate_known_semantics=True
    )


def interpretation_constraint_reason(
    request: SearchRequest,
    text: str,
    *,
    permitted_topic_terms: tuple[str, ...] = (),
) -> str | None:
    """校验解释器的独立问句，同时允许低置信语义升级。

    Args:
        request: 原问题、范围与有限会话上下文。
        text: 模型返回的独立问题。
        permitted_topic_terms: 已由严格 JSON 字段另行校验的对象、来源与关系词。

    Returns:
        表面范围或硬约束被改变时的拒绝原因，否则返回 None。

    """
    reason = (
        _interpretation_surface_reason(request, text, permitted_topic_terms)
        if permitted_topic_terms
        else _surface_constraint_reason(
            request, text, validate_known_semantics=False
        )
    )
    return (
        None if reason is None else reason.replace("REWRITE_", "INTERPRET_", 1)
    )


def _surface_constraint_reason(
    request: SearchRequest,
    text: str,
    *,
    validate_known_semantics: bool,
) -> str | None:
    """共用原词、范围与硬约束校验。"""
    analyzer = QueryAnalyzer()
    original = analyzer.analyze(request)
    rewritten = analyzer.analyze(request.model_copy(update={"text": text}))
    uses_context = bool(
        request.conversation_context and _CONTEXT_REFERENCE.search(request.text)
    )
    context = (
        analyzer.analyze(
            request.model_copy(
                update={
                    "text": "\n".join(request.conversation_context[-8:]),
                    "conversation_context": (),
                }
            )
        )
        if uses_context
        else None
    )
    before = _normalize(request.text)
    after = _normalize(text)
    hard_changed = _hard_fields_changed(
        original,
        rewritten,
        context,
        before=before,
        after=after,
    ) or any(
        _atoms(pattern, before) != _atoms(pattern, after)
        for pattern in (_NEGATION, _QUALIFIER)
    )
    # 字面信号允许调序，不能新增、删除或替换。
    if hard_changed:
        return "REWRITE_CONSTRAINT_CHANGED"
    if validate_known_semantics:
        semantic_reason = _semantic_change_reason(original, rewritten)
        if semantic_reason is not None:
            return semantic_reason
    before_terms = _topics(
        _CONTEXT_REFERENCE.sub("", before) if uses_context else before
    )
    after_terms = _topics(after)
    if not before_terms or not after_terms:
        return None if before == after else "REWRITE_SCOPE_CHANGED"
    if uses_context:
        context_terms = _topics(
            _normalize("\n".join(request.conversation_context[-8:]))
        )
        topics_match = _contextual_topics_match(
            before_terms,
            after_terms,
            context_terms,
        )
    else:
        topics_match = _same_topics(before_terms, after_terms)
    if not topics_match:
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


def _interpretation_surface_reason(
    request: SearchRequest,
    text: str,
    permitted_topic_terms: tuple[str, ...],
) -> str | None:
    """允许把口语问法规范化，但不允许增删业务主题或硬约束。"""
    analyzer = QueryAnalyzer()
    original = analyzer.analyze(request)
    interpreted = analyzer.analyze(request.model_copy(update={"text": text}))
    uses_context = bool(
        request.conversation_context and _CONTEXT_REFERENCE.search(request.text)
    )
    context = (
        analyzer.analyze(
            request.model_copy(
                update={
                    "text": "\n".join(request.conversation_context[-8:]),
                    "conversation_context": (),
                }
            )
        )
        if uses_context
        else None
    )
    before = _normalize(request.text)
    after = _normalize(text)
    if _hard_fields_changed(
        original,
        interpreted,
        context,
        before=before,
        after=after,
    ) or any(
        _atoms(pattern, before) != _atoms(pattern, after)
        for pattern in (_NEGATION, _QUALIFIER)
    ):
        return "REWRITE_CONSTRAINT_CHANGED"
    before_terms = _interpretation_topics(before, permitted_topic_terms)
    after_terms = _interpretation_topics(after, permitted_topic_terms)
    if uses_context:
        context_terms = _interpretation_topics(
            _normalize("\n".join(request.conversation_context[-8:])),
            permitted_topic_terms,
        )
        topics_match = _contextual_topics_match(
            before_terms,
            after_terms,
            context_terms,
        )
    else:
        topics_match = _same_topics(before_terms, after_terms)
    return None if topics_match else "REWRITE_SCOPE_CHANGED"


def _interpretation_topics(
    text: str, permitted_topic_terms: tuple[str, ...]
) -> tuple[str, ...]:
    """移除已单独核验字段和纯口语框架，留下必须双向守恒的主题。"""
    normalized = _normalize(text)
    for term in sorted(
        {_normalize(value) for value in permitted_topic_terms if value},
        key=len,
        reverse=True,
    ):
        normalized = normalized.replace(term, " ")
    return _topics(_INTERPRETATION_FILLER.sub(" ", normalized))


__all__ = ["interpretation_constraint_reason", "rewrite_constraint_reason"]


def _hard_fields_changed(
    original: QueryAnalysis,
    rewritten: QueryAnalysis,
    context: QueryAnalysis | None,
    *,
    before: str,
    after: str,
) -> bool:
    """保留硬约束，只允许从同 scope 会话补入一个完整对象标识。"""
    exact_fields = (
        "units",
        "date_version_signals",
        "negation_signals",
        "quoted_phrases",
    )
    if any(
        Counter(getattr(original, field)) != Counter(getattr(rewritten, field))
        for field in exact_fields
    ):
        return True
    before_identifiers = Counter(original.identifiers)
    after_identifiers = Counter(rewritten.identifiers)
    if context is None:
        identifiers_changed = before_identifiers != after_identifiers
        approved_identifiers: Counter[str] = Counter()
    else:
        available = Counter(context.identifiers)
        approved_identifiers = after_identifiers - before_identifiers
        identifiers_changed = bool(
            (before_identifiers - after_identifiers)
            or (approved_identifiers - available)
        )
    if identifiers_changed:
        return True
    before_atoms = _atoms(_NUMBER, before)
    after_atoms = _atoms(_NUMBER, after)
    if before_atoms - after_atoms:
        return True
    added_atoms = after_atoms - before_atoms
    approved_text = _normalize("".join(approved_identifiers.elements()))
    if any(atom not in approved_text for atom in added_atoms.elements()):
        return True
    before_numbers = Counter(original.numbers)
    after_numbers = Counter(rewritten.numbers)
    if before_numbers - after_numbers:
        return True
    added_numbers = after_numbers - before_numbers
    return any(
        number not in approved_text for number in added_numbers.elements()
    )


def _semantic_change_reason(
    original: QueryAnalysis, rewritten: QueryAnalysis
) -> str | None:
    """对共享语义可确认的问法做精确对象、关系与形状比较。"""
    before = original.semantics
    after = rewritten.semantics
    descriptive = {
        RequestedAnswerType.DEFINITION,
        RequestedAnswerType.PURPOSE,
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.DUTIES,
        RequestedAnswerType.RESPONSIBLE_PARTY,
        RequestedAnswerType.PROCEDURE,
        RequestedAnswerType.SECTION_SUMMARY,
    }
    if before.answer_type in descriptive or after.answer_type in descriptive:
        if before.answer_type is not after.answer_type:
            return "REWRITE_CONSTRAINT_CHANGED"
        if (before.expected_count, before.ordinal) != (
            after.expected_count,
            after.ordinal,
        ):
            return "REWRITE_CONSTRAINT_CHANGED"
        if before.source_qualifier != after.source_qualifier:
            return "REWRITE_SCOPE_CHANGED"
        if (
            before.answer_type
            not in {
                RequestedAnswerType.DUTIES,
                RequestedAnswerType.RESPONSIBLE_PARTY,
            }
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


def _contextual_topics_match(
    before: tuple[str, ...],
    after: tuple[str, ...],
    context: tuple[str, ...],
) -> bool:
    """允许指代被已鉴权上下文对象替换，但拒绝上下文外新增主题。"""
    if not all(_covered(term, after) for term in before):
        return False
    available = (*context, *before)
    return all(_covered(term, available) for term in after)


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
