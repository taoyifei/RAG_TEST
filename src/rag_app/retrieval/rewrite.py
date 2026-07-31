"""有界、条件触发且失败回退原查询的问题改写。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from rag_app.chunking import TokenCounter
from rag_app.clients.llm import (
    BufferedLlmClient,
    ChatMessage,
    LlmGeneration,
)
from rag_app.clients.model_services import ExternalCallAudit
from rag_app.clients.resilience import (
    ExternalRequestRejectedError,
    ExternalServiceUnavailableError,
)
from rag_app.model_contracts import (
    ResolvedReference,
    VerifiedClaimContext,
    parse_rewrite_response,
    rewrite_contract_revision,
    rewrite_request,
    rewrite_response_format,
)
from rag_app.tracing.models import JsonValue
from rag_app.tracing.reasons import DecisionCode

__all__ = [
    "QueryRewriteConfig",
    "QueryRewriter",
    "QueryVariants",
    "RewriteTokenLimitError",
]

_PRONOUN_SIGNALS = (
    "这个",
    "这些",
    "那个",
    "那些",
    "它",
    "其中",
    "上述",
    "前述",
    "前者",
    "后者",
)
_TEMPORAL_SIGNALS = ("刚才",)
_DISCOURSE_REFERENCE_PATTERN = re.compile(
    r"(?:前面|上面|上文)(?:提到(?:的)?|说到(?:的)?|提及(?:的)?|所述|说的)"
)
_PREVIOUS_ITEM_PATTERN = re.compile(r"上一(?:条|项|个)")
_REFERENTIAL_ORDINAL_PATTERN = re.compile(
    r"第(?P<number>\d+|[一二三四五六七八九十百千万两]+)(?:种|项|个)"
)
_FACT_ORDINAL_PATTERN = re.compile(
    r"第(?:\d+|[一二三四五六七八九十百千万两]+)(?:条|章|款)"
)
_ORDINAL_PATTERN = re.compile(
    rf"(?:{_REFERENTIAL_ORDINAL_PATTERN.pattern}|"
    rf"{_FACT_ORDINAL_PATTERN.pattern})"
)
_PAIR_REFERENCE_PATTERN = re.compile(r"前者|后者")
_CONTINUATION_PATTERN = re.compile(
    r"^(?:请)?(?:"
    r"继续(?:$|[，。！？?!\s]|说|说明|介绍|展开|补充|讲|回答)"
    r"|再详细|还有吗|还有么|然后呢|那怎么办"
    r")"
)
_DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{4})(?:年|[-/.])(\d{1,2})(?:月|[-/.])"
    r"(\d{1,2})日?(?!\d)"
)
_PERCENT_PATTERN = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[%％]")
_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9._/-]*[A-Za-z])"
    r"(?=[A-Za-z0-9._/-]*\d)"
    r"[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*"
    r"(?![A-Za-z0-9])"
)
_CLAUSE_PATTERN = re.compile(r"(?<!\d)\d+(?:\.\d+){1,}(?!\d)")
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])"
)
_QUOTED_NAME_PATTERN = re.compile(
    r"“([^”\n]+)”|\"([^\"\n]+)\"|‘([^’\n]+)’"
)
_REWRITTEN_QUERY_COUNT = 2
_CHINESE_SECTION_UNIT = 10_000


@dataclass(frozen=True, slots=True)
class QueryRewriteConfig:
    """改写的轮数与 token 硬上限。"""

    max_history_turns: int
    history_token_budget: int
    max_question_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        """拒绝零值或无界配置。"""
        if (
            min(
                self.max_history_turns,
                self.history_token_budget,
                self.max_question_tokens,
                self.max_output_tokens,
            )
            <= 0
        ):
            raise ValueError("改写轮数和 token 预算必须为正数。")


@dataclass(frozen=True, slots=True)
class QueryVariants:
    """原查询和至多一个独立改写查询。"""

    queries: tuple[str, ...]
    resolved_query: str
    rewritten: bool
    call: ExternalCallAudit | None
    trace: dict[str, JsonValue] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """校验原查询首位和唯一 resolved query 语义。"""
        if not self.queries or not self.resolved_query.strip():
            raise ValueError("查询变体和 resolved_query 不能为空。")
        if self.rewritten:
            if (
                len(self.queries) != _REWRITTEN_QUERY_COUNT
                or self.resolved_query != self.queries[1]
            ):
                raise ValueError("改写成功时 resolved_query 必须是独立问题。")
        elif len(self.queries) != 1 or self.resolved_query != self.queries[0]:
            raise ValueError("未改写时 resolved_query 必须是原问题。")


class RewriteTokenLimitError(ValueError):
    """当前原问题超过改写硬 token 上限。"""

    def __init__(self, trace: dict[str, JsonValue]) -> None:
        super().__init__("当前问题超过改写 token 上限。")
        self.trace = trace


class QueryRewriter:
    """仅对含省略/代词信号的多轮问题做一次结构化改写。"""

    def __init__(
        self,
        llm: BufferedLlmClient,
        token_counter: TokenCounter,
        config: QueryRewriteConfig,
    ) -> None:
        """保存 LLM、模型 tokenizer 与硬预算。

        Args:
            llm: 缓冲生成客户端。
            token_counter: 与 LLM 对应的本地 token 计数器。
            config: 轮数与 token 上限。

        """
        self._llm = llm
        self._token_counter = token_counter
        self._config = config

    def rewrite(  # noqa: PLR0911, PLR0912
        self,
        question: str,
        *,
        previous_questions: tuple[str, ...],
        verified_claims: tuple[VerifiedClaimContext, ...] = (),
    ) -> QueryVariants:
        """按需生成独立查询，任何失败均保留原查询。

        Args:
            question: 当前用户原始问题。
            previous_questions: 只含历史用户问题，不含历史答案。
            verified_claims: 上一轮 AnswerResult 的有序最小摘要。

        Returns:
            原查询，以及可选的合法改写查询。

        Raises:
            ValueError: 当前问题为空或超过硬 token 上限。

        """
        stripped_question = question.strip()
        if not stripped_question:
            raise ValueError("当前问题不能为空。")
        trigger_reason = _context_signal_reason(stripped_question)
        question_tokens = self._token_counter.count(stripped_question)
        if question_tokens > self._config.max_question_tokens:
            raise RewriteTokenLimitError(
                _rewrite_trace(
                    question=stripped_question,
                    history=previous_questions,
                    selected_history=(),
                    resolved_query=stripped_question,
                    reason=DecisionCode.REWRITE_TOKEN_LIMIT,
                    question_tokens=question_tokens,
                    resolved_tokens=question_tokens,
                    token_counter=self._token_counter,
                    messages=(),
                    generated=None,
                    max_output_tokens=self._config.max_output_tokens,
                    trigger_reason=trigger_reason,
                )
            )
        if not previous_questions and not verified_claims:
            return self._original(
                stripped_question,
                previous_questions,
                (),
                DecisionCode.NO_HISTORY,
                question_tokens,
                trigger_reason=trigger_reason,
            )
        if trigger_reason is None:
            return self._original(
                stripped_question,
                previous_questions,
                (),
                DecisionCode.NO_CONTEXT_SIGNAL,
                question_tokens,
                trigger_reason=trigger_reason,
            )
        resolved_references = _resolve_references(
            stripped_question,
            verified_claims,
        )
        if resolved_references is None:
            return self._original(
                stripped_question,
                previous_questions,
                (),
                "REWRITE_CONTEXT_UNRESOLVED",
                question_tokens,
                trigger_reason=trigger_reason,
            )
        selected_history, selected_claims = self._select_context(
            previous_questions,
            verified_claims,
            required_claims=tuple(
                reference.claim for reference in resolved_references
            ),
        )
        selected_claim_ids = {id(claim) for claim in selected_claims}
        if any(
            id(reference.claim) not in selected_claim_ids
            for reference in resolved_references
        ):
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                "REWRITE_CONTEXT_UNRESOLVED",
                question_tokens,
                trigger_reason=trigger_reason,
            )
        if not selected_history and not selected_claims:
            return self._original(
                stripped_question,
                previous_questions,
                (),
                DecisionCode.HISTORY_BUDGET_EMPTY,
                question_tokens,
                trigger_reason=trigger_reason,
            )
        request = rewrite_request(
            stripped_question,
            history_questions=selected_history,
            verified_claims=selected_claims,
            resolved_references=resolved_references,
            max_output_tokens=self._config.max_output_tokens,
        )
        messages = request.messages
        try:
            generated = self._llm.generate(
                messages,
                max_output_tokens=request.max_output_tokens,
                response_format=request.response_format,
            )
        except (
            ExternalRequestRejectedError,
            ExternalServiceUnavailableError,
        ):
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                DecisionCode.REWRITE_MODEL_UNAVAILABLE,
                question_tokens,
                trigger_reason=trigger_reason,
                messages=messages,
            )
        except ValueError:
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                DecisionCode.REWRITE_INVALID_SCHEMA,
                question_tokens,
                trigger_reason=trigger_reason,
                messages=messages,
            )
        try:
            rewritten = parse_rewrite_response(generated.content)
        except (json.JSONDecodeError, ValueError):
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                DecisionCode.REWRITE_INVALID_SCHEMA,
                question_tokens,
                trigger_reason=trigger_reason,
                messages=messages,
                generated=generated,
            )
        if rewritten == stripped_question:
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                DecisionCode.REWRITE_SAME_AS_ORIGINAL,
                question_tokens,
                trigger_reason=trigger_reason,
                messages=messages,
                generated=generated,
            )
        if not _anchors_valid(
            stripped_question,
            selected_history,
            rewritten,
            selected_claims=selected_claims,
            resolved_references=resolved_references,
        ):
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                DecisionCode.REWRITE_ANCHOR_DRIFT,
                question_tokens,
                trigger_reason=trigger_reason,
                messages=messages,
                generated=generated,
            )
        rewritten_tokens = self._token_counter.count(rewritten)
        if rewritten_tokens > self._config.max_question_tokens:
            return self._original(
                stripped_question,
                previous_questions,
                selected_history,
                DecisionCode.REWRITE_TOKEN_LIMIT,
                question_tokens,
                trigger_reason=trigger_reason,
                messages=messages,
                generated=generated,
            )
        return QueryVariants(
            queries=(stripped_question, rewritten),
            resolved_query=rewritten,
            rewritten=True,
            call=generated.call,
            trace=_rewrite_trace(
                question=stripped_question,
                history=previous_questions,
                selected_history=selected_history,
                resolved_query=rewritten,
                reason=DecisionCode.REWRITE_OK,
                question_tokens=question_tokens,
                resolved_tokens=rewritten_tokens,
                token_counter=self._token_counter,
                messages=messages,
                generated=generated,
                max_output_tokens=self._config.max_output_tokens,
                trigger_reason=trigger_reason,
            ),
        )

    def revision(self) -> str:
        """返回唯一模型契约定义的改写 revision。

        Args:
            无参数；使用当前改写器的冻结规则。

        Returns:
            带算法前缀的集中契约 revision。

        """
        return rewrite_contract_revision()

    def _select_context(
        self,
        previous_questions: tuple[str, ...],
        verified_claims: tuple[VerifiedClaimContext, ...],
        *,
        required_claims: tuple[VerifiedClaimContext, ...],
    ) -> tuple[tuple[str, ...], tuple[VerifiedClaimContext, ...]]:
        """在共享 token 预算内选择有序 claims 和最近问题。

        Args:
            previous_questions: 按时间顺序排列的历史问题。
            verified_claims: 上一轮按原回答顺序排列的已验证 claims。
            required_claims: 当前明确回指必须包含的 claims。

        Returns:
            保持原顺序的历史问题和 claim 子序列。

        """
        required_ids = {id(claim) for claim in required_claims}
        candidate_indexes = tuple(
            index
            for index, claim in enumerate(verified_claims)
            if id(claim) in required_ids
        ) + tuple(
            index
            for index, claim in enumerate(verified_claims)
            if id(claim) not in required_ids
        )
        selected_indexes: list[int] = []
        used_tokens = 0
        for index in candidate_indexes:
            claim = verified_claims[index]
            serialized_claim = json.dumps(
                claim.as_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            tokens = self._token_counter.count(serialized_claim)
            if used_tokens + tokens > self._config.history_token_budget:
                continue
            selected_indexes.append(index)
            used_tokens += tokens
        selected_reversed: list[str] = []
        for raw_question in reversed(
            previous_questions[-self._config.max_history_turns :]
        ):
            historical_question = raw_question.strip()
            if not historical_question:
                continue
            tokens = self._token_counter.count(historical_question)
            if used_tokens + tokens > self._config.history_token_budget:
                break
            selected_reversed.append(historical_question)
            used_tokens += tokens
        return (
            tuple(reversed(selected_reversed)),
            tuple(
                verified_claims[index]
                for index in sorted(selected_indexes)
            ),
        )

    def _original(  # noqa: PLR0913
        self,
        question: str,
        history: tuple[str, ...],
        selected_history: tuple[str, ...],
        reason: DecisionCode | str,
        question_tokens: int,
        *,
        trigger_reason: DecisionCode | None,
        messages: tuple[ChatMessage, ...] = (),
        generated: LlmGeneration | None = None,
    ) -> QueryVariants:
        """构造回退到原问题的查询变体及诊断信息。

        Args:
            question: 已规范化的当前问题。
            history: 调用方提供的完整历史问题。
            selected_history: 在 token 预算内选中的历史问题。
            reason: 不采用改写结果的稳定原因码。
            question_tokens: 原问题的 token 数量。
            trigger_reason: 命中的确定性触发规则类别。
            messages: 已发送给模型的消息；未调用模型时为空。
            generated: 可选的模型生成结果。

        Returns:
            只包含原问题且标记为未改写的查询变体。

        """
        return QueryVariants(
            queries=(question,),
            resolved_query=question,
            rewritten=False,
            call=None if generated is None else generated.call,
            trace=_rewrite_trace(
                question=question,
                history=history,
                selected_history=selected_history,
                resolved_query=question,
                reason=reason,
                question_tokens=question_tokens,
                resolved_tokens=question_tokens,
                token_counter=self._token_counter,
                messages=messages,
                generated=generated,
                max_output_tokens=self._config.max_output_tokens,
                trigger_reason=trigger_reason,
            ),
        )


def _context_signal_reason(question: str) -> DecisionCode | None:
    if any(signal in question for signal in _PRONOUN_SIGNALS):
        return DecisionCode.REWRITE_TRIGGER_PRONOUN
    if (
        any(signal in question for signal in _TEMPORAL_SIGNALS)
        or _DISCOURSE_REFERENCE_PATTERN.search(question) is not None
        or _PREVIOUS_ITEM_PATTERN.search(question) is not None
    ):
        return DecisionCode.REWRITE_TRIGGER_TEMPORAL
    if _ORDINAL_PATTERN.search(question) is not None:
        return DecisionCode.REWRITE_TRIGGER_ORDINAL
    if _CONTINUATION_PATTERN.search(question) is not None:
        return DecisionCode.REWRITE_TRIGGER_CONTINUATION
    return None


def _anchors_valid(
    question: str,
    selected_history: tuple[str, ...],
    rewritten: str,
    *,
    selected_claims: tuple[VerifiedClaimContext, ...],
    resolved_references: tuple[ResolvedReference, ...],
) -> bool:
    question_anchors = _extract_anchors(question)
    rewritten_anchors = _extract_anchors(rewritten)
    history_anchors = frozenset(
        anchor
        for historical_question in selected_history
        for anchor in _extract_anchors(historical_question)
    )
    claim_anchors = frozenset(
        anchor
        for claim in selected_claims
        for anchor in _extract_anchors(claim.text)
    )
    if any(
        not _anchor_present(anchor, rewritten_anchors, rewritten)
        for anchor in question_anchors
    ):
        return False
    if any(
        reference.claim.text.strip().casefold()
        not in rewritten.casefold()
        for reference in resolved_references
    ):
        return False
    return (
        rewritten_anchors
        <= question_anchors | history_anchors | claim_anchors
    )


def _extract_anchors(text: str) -> frozenset[str]:
    anchors: set[str] = set()
    for match in _DATE_PATTERN.finditer(text):
        year, month, day = (
            _normalize_number(value) for value in match.groups()
        )
        anchors.add(f"date:{year}-{month}-{day}")
    anchors.update(
        f"percent:{_normalize_number(match.group(1))}"
        for match in _PERCENT_PATTERN.finditer(text)
    )
    anchors.update(
        f"identifier:{match.group(0).casefold()}"
        for match in _IDENTIFIER_PATTERN.finditer(text)
    )
    anchors.update(
        f"clause:{match.group(0)}"
        for match in _CLAUSE_PATTERN.finditer(text)
    )
    anchors.update(
        f"number:{_normalize_number(match.group(0))}"
        for match in _NUMBER_PATTERN.finditer(text)
    )
    anchors.update(
        f"ordinal:{match.group(0)}"
        for match in _FACT_ORDINAL_PATTERN.finditer(text)
    )
    for match in _QUOTED_NAME_PATTERN.finditer(text):
        name = next(
            value.strip() for value in match.groups() if value is not None
        )
        if name:
            anchors.add(f"name:{name.casefold()}")
    return frozenset(anchors)


def _anchor_present(
    anchor: str,
    rewritten_anchors: frozenset[str],
    rewritten: str,
) -> bool:
    if anchor.startswith("name:"):
        return anchor.removeprefix("name:") in rewritten.casefold()
    return anchor in rewritten_anchors


def _normalize_number(value: str) -> str:
    integer, separator, fraction = value.partition(".")
    normalized_integer = str(int(integer))
    normalized_fraction = fraction.rstrip("0")
    if separator and normalized_fraction:
        return f"{normalized_integer}.{normalized_fraction}"
    return normalized_integer


def _resolve_references(
    question: str,
    claims: tuple[VerifiedClaimContext, ...],
) -> tuple[ResolvedReference, ...] | None:
    """把回指序号和前者/后者确定性映射到已验证 claim。"""
    references = sorted(
        (
            (match.start(), match.group(0), _ordinal_index(match))
            for match in _REFERENTIAL_ORDINAL_PATTERN.finditer(question)
        ),
        key=lambda item: item[0],
    )
    references.extend(
        (
            match.start(),
            match.group(0),
            len(claims) - 2 if match.group(0) == "前者" else len(claims) - 1,
        )
        for match in _PAIR_REFERENCE_PATTERN.finditer(question)
    )
    resolved: list[ResolvedReference] = []
    for _, reference, claim_index in sorted(
        references,
        key=lambda item: item[0],
    ):
        if claim_index < 0 or claim_index >= len(claims):
            return None
        resolved.append(
            ResolvedReference(
                reference=reference,
                claim=claims[claim_index],
            )
        )
    return tuple(resolved)


def _ordinal_index(match: re.Match[str]) -> int:
    """把第 N 种/项/个转换为从零开始的 claim 下标。"""
    raw_number = match.group("number")
    ordinal = (
        int(raw_number)
        if raw_number.isdigit()
        else _parse_chinese_number(raw_number)
    )
    return ordinal - 1


def _parse_chinese_number(value: str) -> int:
    """解析改写回指所允许的中文整数。"""
    digits = {
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
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    section = 0
    digit = 0
    for character in value:
        if character in digits:
            digit = digits[character]
            continue
        unit = units[character]
        if unit == _CHINESE_SECTION_UNIT:
            total += (section + digit) * unit
            section = 0
        else:
            section += (digit or 1) * unit
        digit = 0
    return total + section + digit


def _rewrite_trace(  # noqa: PLR0913
    *,
    question: str,
    history: tuple[str, ...],
    selected_history: tuple[str, ...],
    resolved_query: str,
    reason: DecisionCode | str,
    question_tokens: int,
    resolved_tokens: int,
    token_counter: TokenCounter,
    messages: tuple[ChatMessage, ...],
    generated: LlmGeneration | None,
    max_output_tokens: int,
    trigger_reason: DecisionCode | None,
) -> dict[str, JsonValue]:
    """构造查询改写决策的完整诊断属性。

    Args:
        question: 已规范化的当前问题。
        history: 调用方提供的完整历史问题。
        selected_history: 在 token 预算内选中的历史问题。
        resolved_query: 最终进入检索的独立查询。
        reason: 改写或回退的稳定原因码。
        question_tokens: 原问题 token 数量。
        resolved_tokens: 最终查询 token 数量。
        token_counter: 冻结模型对应的 token 计数器。
        messages: 实际发送给模型的消息。
        generated: 可选的模型生成结果。
        max_output_tokens: 改写调用的输出 token 上限。
        trigger_reason: 命中的确定性触发规则类别。

    Returns:
        包含摘要、token 计数、输入和可选模型响应的 Trace 属性。

    """
    history_json = json.dumps(
        history,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    reason_value = _decision_value(reason)
    persisted_reason = (
        trigger_reason
        if reason == DecisionCode.REWRITE_OK and trigger_reason is not None
        else None
    )
    trace: dict[str, JsonValue] = {
        "reason_code": (
            reason_value
            if persisted_reason is None
            else persisted_reason.value
        ),
        "rewrite_result_code": reason_value,
        "trigger_reason_code": (
            None if trigger_reason is None else trigger_reason.value
        ),
        "question_sha256": _sha256(question),
        "history_sha256": _sha256(history_json),
        "resolved_query_sha256": _sha256(resolved_query),
        "question_tokens": question_tokens,
        "history_tokens": sum(token_counter.count(item) for item in history),
        "selected_history_tokens": sum(
            token_counter.count(item) for item in selected_history
        ),
        "resolved_query_tokens": resolved_tokens,
        "question": question,
        "history": list(history),
        "selected_history": list(selected_history),
        "resolved_query": resolved_query,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in messages
        ],
        "response_format": rewrite_response_format(),
        "max_output_tokens": max_output_tokens,
    }
    if generated is not None:
        trace.update(
            {
                "raw_output": generated.content,
                "model": generated.model,
                "prompt_tokens": generated.usage.prompt_tokens,
                "completion_tokens": generated.usage.completion_tokens,
                "total_tokens": generated.usage.total_tokens,
                "endpoint": generated.call.endpoint,
                "retry_count": generated.call.retry_count,
            }
        )
    return trace


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decision_value(reason: DecisionCode | str) -> str:
    """返回 enum 或本模块稳定扩展原因码的字符串值。"""
    return reason.value if isinstance(reason, DecisionCode) else reason
