"""从受信用户问题构造可审计的多轮检索根问句。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum

from pydantic import Field

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import QueryAnalysis, SearchRequest
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query_plan import (
    QueryAtom,
    QueryPlan,
    ReasoningEffortValue,
    fallback_query_plan,
    make_query_plan,
)

CONTEXT_RESOLUTION_REVISION = "wb08r-context-resolution-v4"
_CLAUSES = re.compile(r"[^，,；;。！？?!]+")
_REFERENCES = re.compile(r"这个|那个|上述|前者|后者|其中|它|其|这些|那些|那")
_SHORT_RELATION = re.compile(
    r"多久|何时|什么时候|多少|谁|哪里|怎么|如何|哪些|什么"
)
_CONTEXT_MODIFIER = re.compile(
    r"^(?:(?:根据|依据|按照)《[^》]+》|.+从.+到.+(?:后|前|期间))$"
)
_INTERROGATIVE_CLAUSE = re.compile(
    r"谁|什么|啥|哪些|哪(?:个|些|里|一)|多少|多久|怎么|如何|怎样|"
    r"几(?:个)?(?:工作日|自然日|日|天|小时|分钟|周|月|年)|"
    r"何时|什么时候|是否|能否|可否|[吗呢]$|不$"
)
_TARGET_BEFORE_QUESTION = re.compile(
    r"^(.+?)(?:什么时候|何时|多久|多少|怎么|如何|是什么|有哪些|"
    r"负责什么|需要什么|需要哪些|包括哪些|包含哪些|由谁|谁负责)"
)
_TARGET_BEFORE_STAGE_RANGE = re.compile(
    r"^(?P<target>[^，,；;。！？?!]{2,80}?)从[^，,；;。！？?!]+到"
)
_QUESTION_RELATION = re.compile(
    r"(?:多长时间内|多长时间|什么时候|哪些|什么|如何|怎么|怎样|多久|"
    r"何时|多少|几天|几日)(?P<relation>[^，,；;。！？?!]+)$"
)
_DECLARATIVE_ACTION = re.compile(
    r"准备|计划|打算|申请|办理|提交|采购|签订|使用|参加|开展|"
    r"更换|退出|讨论|说到|提到|涉及|关注|做"
)
_STATE_PIVOT = re.compile(r"还没|尚未|已经|正在|正要|需要|应当|必须|可以|要")
_DISCOURSE_PREFIX = re.compile(r"^(?:并且|而且|同时|然后|那|这|还|再|又)")
_PRONOUN_TARGET = re.compile(r"^(?:其|它|该|这个|那个|上述|其中|前者|后者)")
_MULTIPLE = re.compile(
    r"[^，,；;。！？?!、]{1,80}(?:、[^，,；;。！？?!、]{1,80})+"
)
_TIME_MODIFIER = re.compile(
    r"^(提前|之后|之前|以后|以前|还|再|又|那|这|同时|同时在)$"
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_NEGATION = re.compile(r"不得|无需|不必|禁止|严禁|没有|未|不(?![呢吗呀啊]?$)")
_SEQUENCE = re.compile(r"先|再|然后|随后")
_MIN_SEQUENCE_PARTS = 2
_MAX_ATOMS = 4
_MAX_SHORT_FOLLOW_UP_CHARS = 24
_MIN_TARGET_CHARS = 2
_INDEPENDENT_TOPIC_PREFIX = re.compile(
    r"^(?P<topic>[^，,；;。！？?!]{2,80}?)(?:需要|应当|必须|要|须)"
)


def _question_clauses(normalized: str) -> tuple[str, ...]:
    """顿号两侧各自成问时才拆句；枚举的宾语仍属同一问句。"""
    raw = tuple(
        match[0].strip()
        for match in _CLAUSES.finditer(normalized)
        if match[0].strip()
    )
    clauses: list[str] = []
    for clause in raw:
        parts = tuple(part.strip() for part in clause.split("、"))
        if len(parts) > 1 and all(
            part and _INTERROGATIVE_CLAUSE.search(part) for part in parts
        ):
            clauses.extend(parts)
        else:
            clauses.append(clause)
    return tuple(clauses)


class SpanKind(StrEnum):
    """用户输入片段的通用语义类别。"""

    CLAUSE = "CLAUSE"
    TARGET = "TARGET"
    RELATION = "RELATION"
    SOURCE = "SOURCE"
    LITERAL = "LITERAL"


class QueryInputSpan(FrozenModel):
    """服务端从当前或先前用户问句切出的受信片段。"""

    span_id: str = Field(min_length=3, max_length=16)
    turn: str
    kind: SpanKind
    text: str = Field(min_length=1, max_length=160, repr=False)
    normalized_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ResolvedRootQuery(FrozenModel):
    """原问和确定性组合的 Root 查询同时存在。"""

    original_query: str = Field(min_length=1, repr=False)
    resolved_query: str = Field(min_length=1, max_length=512, repr=False)
    mode: str
    confidence: str
    referenced_span_ids: tuple[str, ...] = ()
    context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolution_revision: str = CONTEXT_RESOLUTION_REVISION
    reason_codes: tuple[str, ...] = ()


def trusted_user_questions(context: tuple[str, ...]) -> tuple[str, ...]:
    """会话密文中只提取用户问句，绝不读入模型回答。"""
    questions: list[str] = []
    for turn in context[-2:]:
        first_line = turn.splitlines()[0].strip()
        if not first_line.startswith("上一问："):
            continue
        first_line = first_line.removeprefix("上一问：").strip()
        if first_line:
            questions.append(first_line)
    return tuple(questions)


def build_input_spans(  # noqa: PLR0912, PLR0915
    request: SearchRequest,
) -> tuple[QueryInputSpan, ...]:
    """从当前问题和最多两轮用户问句构造稳定 ID。"""
    questions = (
        request.text,
        *reversed(trusted_user_questions(request.conversation_context)),
    )
    spans: list[QueryInputSpan] = []
    for turn_index, question in enumerate(questions):
        prefix = "Q" if turn_index == 0 else f"P{turn_index}"
        turn = "CURRENT" if turn_index == 0 else f"PREVIOUS_{turn_index}"
        whole_analysis = QueryAnalyzer().analyze(
            request.model_copy(
                update={"text": question, "conversation_context": ()}
            )
        )
        normalized = (
            whole_analysis.resolved_query or whole_analysis.normalized_query
        ).strip()
        clauses = _question_clauses(normalized)
        _append(spans, prefix, turn, SpanKind.CLAUSE, clauses or (normalized,))
        if whole_analysis.semantics.source_qualifier:
            _append(
                spans,
                prefix,
                turn,
                SpanKind.SOURCE,
                (whole_analysis.semantics.source_qualifier,),
            )
        for clause_index, clause in enumerate(clauses or (normalized,)):
            analyzed = QueryAnalyzer().analyze(
                request.model_copy(
                    update={"text": clause, "conversation_context": ()}
                )
            )
            semantics = analyzed.semantics
            targets: list[str] = []
            if (
                semantics.target
                and semantics.target in clause
                and "分别" not in semantics.target
                and not _PRONOUN_TARGET.match(_clean_target(semantics.target))
            ):
                targets.append(semantics.target)
            syntax_target = _TARGET_BEFORE_QUESTION.search(clause)
            if syntax_target:
                candidate = _clean_target(syntax_target[1])
                if (
                    candidate
                    and not _TIME_MODIFIER.fullmatch(candidate)
                    and "、" not in candidate
                    and "分别" not in candidate
                    and not _PRONOUN_TARGET.match(candidate)
                ):
                    targets.append(candidate)
            stage_target = _TARGET_BEFORE_STAGE_RANGE.search(clause)
            if stage_target:
                candidate = _clean_target(stage_target["target"])
                if candidate and not _PRONOUN_TARGET.match(candidate):
                    targets.append(candidate)
            if turn_index > 0 and not targets:
                declarative = _declarative_target(clause)
                if declarative:
                    targets.append(declarative)
            if "不得" in clause:
                actor = clause.split("不得", 1)[0].strip()
                if actor:
                    targets.append(actor)
            if "先" in clause:
                actor = clause.split("先", 1)[0].strip()
                if actor:
                    targets.append(actor)
            # 并列问句中的对象以原文位置分开，Planner 仍只能引用这些片段。
            before_relation = clause.split("分别", 1)[0]
            if re.search(r"分别|各自", normalized) and _MULTIPLE.search(
                before_relation
            ):
                targets.extend(
                    part.strip()
                    for part in before_relation.split("、")
                    if part.strip()
                )
            current_targets = tuple(
                span
                for span in spans
                if span.turn == turn and span.kind is SpanKind.TARGET
            )
            only_fallback_targets = bool(current_targets) and all(
                span.text in clauses[:clause_index] for span in current_targets
            )
            if (
                not targets
                and turn_index == 0
                and (not current_targets or only_fallback_targets)
                and not _PRONOUN_TARGET.match(_clean_target(clause))
            ):
                # 没有可可靠切分的对象时保留原 Clause，供 Planner 选择；
                # 后续证据发布仍要求来源直接证明目标关系。
                targets.append(clause)
            _append(
                spans,
                prefix,
                turn,
                SpanKind.TARGET,
                tuple(
                    target
                    for target in targets
                    if not any(
                        target != other and target in other for other in targets
                    )
                ),
            )
            _append(
                spans,
                prefix,
                turn,
                SpanKind.RELATION,
                (_relation_fragment(clause, semantics.relation, targets),),
            )
            if len(_SEQUENCE.findall(clause)) >= _MIN_SEQUENCE_PARTS:
                _append(
                    spans,
                    prefix,
                    turn,
                    SpanKind.RELATION,
                    tuple(
                        part.strip()
                        for part in _SEQUENCE.split(clause)
                        if part.strip() and _SHORT_RELATION.search(part)
                    ),
                )
            if (
                semantics.source_qualifier
                and semantics.source_qualifier in clause
            ):
                _append(
                    spans,
                    prefix,
                    turn,
                    SpanKind.SOURCE,
                    (semantics.source_qualifier,),
                )
            _append(
                spans,
                prefix,
                turn,
                SpanKind.LITERAL,
                (
                    *analyzed.identifiers,
                    *analyzed.quoted_phrases,
                    *analyzed.date_version_signals,
                    *analyzed.numbers,
                    *analyzed.units,
                    *analyzed.negation_signals,
                    *_NUMBER.findall(clause),
                    *_NEGATION.findall(clause),
                ),
            )
    return tuple(spans)


def resolve_root_query(
    request: SearchRequest, spans: tuple[QueryInputSpan, ...]
) -> ResolvedRootQuery:
    """仅在先行对象唯一时组合短追问；歧义交给服务端澄清。"""
    original_analysis = QueryAnalyzer().analyze(request)
    original = unicodedata.normalize(
        "NFKC",
        original_analysis.resolved_query or original_analysis.normalized_query,
    ).strip()
    digest = canonical_sha256(
        trusted_user_questions(request.conversation_context)
    )
    current_relation = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.RELATION
    )
    previous_targets = tuple(
        span
        for span in spans
        if span.turn == "PREVIOUS_1" and span.kind is SpanKind.TARGET
    )
    previous_clauses = tuple(
        span
        for span in spans
        if span.turn == "PREVIOUS_1" and span.kind is SpanKind.CLAUSE
    )
    latest_clause = previous_clauses[-1] if previous_clauses else None
    current_clauses = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.CLAUSE
    )
    current_antecedent_candidates = tuple(
        span
        for span in spans
        if span.turn == "CURRENT"
        and span.kind is SpanKind.TARGET
        and current_clauses
        and span.text in current_clauses[0].text
        and not _PRONOUN_TARGET.match(span.text)
    )
    current_antecedents = tuple(
        span
        for span in current_antecedent_candidates
        if not any(
            span.text != other.text and span.text in other.text
            for other in current_antecedent_candidates
        )
    )
    reference = _REFERENCES.search(original)
    local_topic_before_reference = (
        reference is not None
        and len(_HAN.findall(original[: reference.start()]))
        >= _MIN_TARGET_CHARS
        and len({span.text for span in current_antecedents}) == 1
    )
    independent_topic = bool(current_clauses) and any(
        span.text != current_clauses[0].text
        and len(_HAN.findall(span.text)) >= _MIN_TARGET_CHARS
        and not _REFERENCES.match(span.text)
        and not _TIME_MODIFIER.fullmatch(span.text)
        for span in current_antecedents
    )
    if not independent_topic:
        topic_prefix = _INDEPENDENT_TOPIC_PREFIX.match(original)
        independent_topic = bool(
            topic_prefix
            and len(_HAN.findall(topic_prefix["topic"])) >= _MIN_TARGET_CHARS
            and not _REFERENCES.match(topic_prefix["topic"])
        )
    local_topic = local_topic_before_reference or (
        len(current_clauses) > 1
        and len({span.text for span in current_antecedents}) == 1
    )
    if independent_topic or (not previous_targets and local_topic):
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=original[:512],
            mode="ORIGINAL",
            confidence="HIGH",
            context_digest=digest,
            reason_codes=(
                ("CURRENT_TOPIC_STANDALONE",) if independent_topic else ()
            ),
        )
    latest_targets = tuple(
        span
        for span in previous_targets
        if latest_clause is not None and span.text in latest_clause.text
    )
    antecedents = latest_targets or previous_targets
    short_follow_up = len(original) <= _MAX_SHORT_FOLLOW_UP_CHARS and bool(
        _SHORT_RELATION.search(original)
        or original.endswith(("吗", "吗?", "吗？", "?", "？"))
    )
    needs_context = bool(antecedents) and (
        bool(_REFERENCES.search(original)) or short_follow_up
    )
    if not antecedents and bool(_REFERENCES.search(original)):
        needs_context = True
    if not needs_context:
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=original[:512],
            mode="ORIGINAL",
            confidence="HIGH",
            context_digest=digest,
        )
    unique_targets = tuple(dict.fromkeys(span.text for span in antecedents))
    if len(unique_targets) != 1 or not current_relation:
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=original[:512],
            mode="CLARIFY",
            confidence="LOW",
            context_digest=digest,
            reason_codes=("CONTEXT_TARGET_AMBIGUOUS",),
        )
    target_span = next(
        span for span in antecedents if span.text == unique_targets[0]
    )
    current_source = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.SOURCE
    )
    previous_source = tuple(
        span
        for span in spans
        if span.turn == "PREVIOUS_1" and span.kind is SpanKind.SOURCE
    )
    if (
        current_source
        and previous_source
        and current_source[0].text != previous_source[0].text
    ):
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=original[:512],
            mode="CLARIFY",
            confidence="LOW",
            context_digest=digest,
            reason_codes=("CONTEXT_SOURCE_CONFLICT",),
        )
    selected = (
        *current_source,
        target_span,
        *((latest_clause,) if latest_clause is not None else ()),
        *current_relation[:1],
        *(
            span
            for span in spans
            if span.turn == "CURRENT" and span.kind is SpanKind.LITERAL
        ),
    )
    parts = tuple(dict.fromkeys(span.text for span in selected))
    resolved = " ".join((*parts, original))[:512]
    return ResolvedRootQuery(
        original_query=request.text,
        resolved_query=resolved,
        mode="RULE_CONTEXT",
        confidence="HIGH",
        referenced_span_ids=tuple(span.span_id for span in selected),
        context_digest=digest,
        reason_codes=("CONTEXT_TARGET_RESOLVED",),
    )


def _clean_target(value: str) -> str:
    """只移除问句连接成分，不改写用户的实体字面值。"""
    candidate = value.strip("，,；;。！？?!、 ")
    candidate = _DISCOURSE_PREFIX.sub("", candidate)
    return candidate.removesuffix("要").strip()


def _relation_fragment(
    clause: str, analyzed_relation: str | None, targets: list[str]
) -> str:
    """只裁剪用户问句中的关系片段，不生成同义词或答案词。"""
    question = _QUESTION_RELATION.search(clause)
    if question is not None:
        suffix = question["relation"].strip()
        if suffix and suffix not in {"吗", "呢"}:
            return suffix[:160]
    if analyzed_relation and analyzed_relation not in targets:
        return analyzed_relation[:160]
    return clause[:160]


def _declarative_target(clause: str) -> str | None:
    """从先前用户陈述中取最后一个动作对象或状态主体。"""
    actions = tuple(_DECLARATIVE_ACTION.finditer(clause))
    if actions:
        candidate = _clean_target(clause[actions[-1].end() :])
        if len(candidate) >= _MIN_TARGET_CHARS and not _PRONOUN_TARGET.match(
            candidate
        ):
            return candidate
    state = _STATE_PIVOT.search(clause)
    if state:
        candidate = _clean_target(clause[: state.start()])
        if len(candidate) >= _MIN_TARGET_CHARS and not _PRONOUN_TARGET.match(
            candidate
        ):
            return candidate
    return None


def _append(
    spans: list[QueryInputSpan],
    prefix: str,
    turn: str,
    kind: SpanKind,
    values: tuple[str, ...] | list[str],
) -> None:
    seen = {
        span.text for span in spans if span.turn == turn and span.kind is kind
    }
    for value in values:
        text = unicodedata.normalize("NFKC", value).strip()[:160]
        if not text or text in seen:
            continue
        seen.add(text)
        ordinal = (
            sum(span.turn == turn and span.kind is kind for span in spans) + 1
        )
        span_id = f"{prefix}.{kind.value[0]}{ordinal}"
        spans.append(
            QueryInputSpan(
                span_id=span_id,
                turn=turn,
                kind=kind,
                text=text,
                normalized_sha256="sha256:"
                + hashlib.sha256(text.casefold().encode()).hexdigest(),
            )
        )


def current_context_modifier_clauses(
    current_clauses: tuple[QueryInputSpan, ...],
) -> tuple[QueryInputSpan, ...]:
    """识别只限定后续问句、无需独立回答的当前轮分句。"""
    has_interrogative_clause = any(
        _INTERROGATIVE_CLAUSE.search(span.text) for span in current_clauses
    )
    return tuple(
        span
        for index, span in enumerate(current_clauses)
        if _CONTEXT_MODIFIER.fullmatch(span.text)
        or (
            has_interrogative_clause
            and index < len(current_clauses) - 1
            and _INTERROGATIVE_CLAUSE.search(span.text) is None
        )
    )


def degraded_query_plan(  # noqa: PLR0913
    request: SearchRequest,
    analysis: QueryAnalysis,
    spans: tuple[QueryInputSpan, ...],
    root: ResolvedRootQuery,
    *,
    effort: ReasoningEffortValue,
    reason_code: str,
    planner_called: bool,
) -> QueryPlan:
    """Planner 失败时保留可机械切分的子问题和低置信覆盖。"""
    analyzed = analysis
    if root.mode == "CLARIFY":
        base = fallback_query_plan(
            analyzed,
            effort=effort,
            reason_code=reason_code,
            planner_called=planner_called,
            original_query=request.text,
            resolved_root_query=root.resolved_query,
            context_resolution_mode=root.mode,
            context_digest=root.context_digest,
            fallback_mode="DEGRADED_CLARIFY",
            coverage_confidence="LOW",
        )
        return base.model_copy(
            update={
                "needs_clarification": True,
                "clarification_question": "请明确您所指的对象和要查询的事项。",
            }
        )
    clauses = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.CLAUSE
    )
    modifier_clauses = current_context_modifier_clauses(clauses)
    answer_clauses = (
        tuple(span for span in clauses if span not in modifier_clauses)
        or clauses
    )
    targets = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.TARGET
    )
    atoms: list[QueryAtom] = []
    if len(clauses) > 1 or len(targets) > 1:
        for clause in answer_clauses:
            clause_targets: tuple[QueryInputSpan | None, ...] = tuple(
                span for span in targets if span.text in clause.text
            )
            if not clause_targets:
                clause_targets = (None,)
            for target in clause_targets:
                if len(atoms) == _MAX_ATOMS:
                    break
                scoped = QueryAnalyzer().analyze(
                    request.model_copy(
                        update={"text": clause.text, "conversation_context": ()}
                    )
                )
                fallback = fallback_query_plan(
                    scoped,
                    effort=effort,
                    reason_code=reason_code,
                    planner_called=planner_called,
                )
                atom = fallback.atoms[0].model_copy(
                    update={
                        "atom_id": f"A{len(atoms) + 1}",
                        "target": target.text
                        if target
                        else (scoped.semantics.target or clause.text[:160]),
                        "relation": scoped.semantics.relation
                        or next(
                            (
                                span.text
                                for span in spans
                                if span.turn == "CURRENT"
                                and span.kind is SpanKind.RELATION
                                and span.text in clause.text
                            ),
                            clause.text[:160],
                        ),
                        "original_fragment": " ".join(
                            (
                                *(span.text for span in modifier_clauses),
                                clause.text,
                            )
                        )[:320],
                    }
                )
                atoms.append(atom)
        if atoms:
            return make_query_plan(
                standalone_query=root.resolved_query,
                original_query=request.text,
                context_resolution_mode=root.mode,
                context_digest=root.context_digest,
                referenced_span_ids=root.referenced_span_ids,
                intent="COMPOUND" if len(atoms) > 1 else "SINGLE",
                effort=effort,
                atoms=tuple(atoms),
                reason_code=reason_code,
                planner_called=planner_called,
                fallback_mode="DEGRADED_RULE_ATOMS",
                coverage_confidence="LOW",
            )
    return fallback_query_plan(
        analyzed,
        effort=effort,
        reason_code=reason_code,
        planner_called=planner_called,
        original_query=request.text,
        resolved_root_query=root.resolved_query,
        context_resolution_mode=root.mode,
        context_digest=root.context_digest,
        referenced_span_ids=root.referenced_span_ids,
        fallback_mode="DEGRADED_RESOLVED_ROOT",
        coverage_confidence="LOW",
    )
