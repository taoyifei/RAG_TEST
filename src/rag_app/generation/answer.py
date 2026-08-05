"""逐 claim 引文校验、空回答复核与最多一次修复。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import cast

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
from rag_app.generation.evidence import (
    AnswerabilityDecision,
    AnswerabilityStatus,
    EvidenceBundle,
    EvidenceUnit,
    decide_answerability,
)
from rag_app.generation.question_intent import (
    QuestionIntent,
    classify_question_intent,
)
from rag_app.model_contracts import (
    StructuredModelRequest,
    abstention_review_request,
    answer_contract_revision,
    answer_request,
    parse_answer_response,
    repair_answer_request,
)
from rag_app.tracing.models import JsonValue

__all__ = [
    "AnswerClaim",
    "AnswerConfig",
    "AnswerGenerator",
    "AnswerMode",
    "AnswerResult",
    "AnswerStatus",
    "ClaimSupport",
    "RefusalCode",
]

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?%?")
_LONG_CHINESE_TERM_LENGTH = 4


class AnswerStatus(StrEnum):
    """回答发布状态。"""

    ANSWERED = "answered"
    REFUSED = "refused"


class AnswerMode(StrEnum):
    """面向 API 和前端的稳定回答方式。"""

    ANSWERED = "ANSWERED"
    PARTIAL = "PARTIAL"
    CONFLICT = "CONFLICT"
    EXTRACTIVE_FALLBACK = "EXTRACTIVE_FALLBACK"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_VALIDATION_ERROR = "INTERNAL_VALIDATION_ERROR"


class RefusalCode(StrEnum):
    """不含原问题或原文的稳定拒答原因。"""

    NO_EVIDENCE = "NO_EVIDENCE"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    LOW_CONFIDENCE_OCR_ONLY = "LOW_CONFIDENCE_OCR_ONLY"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"


@dataclass(frozen=True, slots=True)
class AnswerConfig:
    """首次生成与唯一修复的输出 token 上限。"""

    max_output_tokens: int
    max_repair_tokens: int

    def __post_init__(self) -> None:
        """拒绝无界输出。"""
        if self.max_output_tokens <= 0 or self.max_repair_tokens <= 0:
            raise ValueError("回答与修复 token 上限必须为正数。")


@dataclass(frozen=True, slots=True)
class ClaimSupport:
    """已验证存在于本次原文证据的支持片段。"""

    evidence_id: str
    chunk_id: str
    quote: str
    locator: str


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    """可发布的一条事实 claim。"""

    text: str
    supports: tuple[ClaimSupport, ...]


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """只包含已校验回答或稳定拒答码。"""

    status: AnswerStatus
    answer: str | None
    claims: tuple[AnswerClaim, ...]
    refusal_code: RefusalCode | None
    model_calls: int
    calls: tuple[ExternalCallAudit, ...]
    answer_mode: AnswerMode = AnswerMode.ANSWERED
    user_message: str | None = None
    trace: dict[str, JsonValue] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


class _ValidationError(ValueError):
    """模型结构化输出未通过确定性证据门禁。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AnswerGenerator:
    """缓冲生成，经确定性验证和至多一次二次调用后发布。"""

    def __init__(
        self,
        llm: BufferedLlmClient,
        config: AnswerConfig,
    ) -> None:
        """保存缓冲 LLM 与输出上限。

        Args:
            llm: 收到完整内容后不会自动重放的客户端。
            config: 首次与修复输出预算。

        """
        self._llm = llm
        self._config = config

    def answer(  # noqa: PLR0911
        self,
        question: str,
        evidence: EvidenceBundle,
        *,
        rerank_scores: tuple[float, ...] = (),
    ) -> AnswerResult:
        """回答当前问题，未通过门禁时返回稳定拒答。

        Args:
            question: 当前原始问题。
            evidence: 已隔离注入并满足 token 预算的证据包。
            rerank_scores: 按精排顺序排列的候选分数。

        Returns:
            可安全发布的回答或拒答。

        """
        if not question.strip():
            raise ValueError("question 不能为空。")
        if not evidence.items:
            code = (
                RefusalCode.PROMPT_INJECTION
                if evidence.quarantined_chunk_ids
                else RefusalCode.NO_EVIDENCE
            )
            return _refusal(code, model_calls=0, calls=())
        if all(item.low_confidence_ocr for item in evidence.items):
            return _refusal(
                RefusalCode.LOW_CONFIDENCE_OCR_ONLY,
                model_calls=0,
                calls=(),
            )
        answerability = decide_answerability(
            question,
            evidence,
            rerank_scores=rerank_scores,
        )
        if answerability.status is AnswerabilityStatus.NOT_FOUND:
            return _refusal(
                RefusalCode.EVIDENCE_INSUFFICIENT,
                model_calls=0,
                calls=(),
                answer_mode=AnswerMode.NOT_FOUND,
                user_message=(
                    "知识库中暂未找到能够支持该问题的资料。请核对项目名称、"
                    "编号或时间，或补充相关文档。"
                ),
                trace=_answerability_trace(answerability),
            )
        first_request = answer_request(
            question,
            evidence_bundle=json.loads(evidence.rendered_json),
            max_output_tokens=self._config.max_output_tokens,
        )
        trace_context = {
            **_answer_trace_context(first_request, evidence),
            **_answerability_trace(answerability),
        }
        messages = first_request.messages
        calls: list[ExternalCallAudit] = []
        generations: list[JsonValue] = []
        try:
            first = self._llm.generate(
                messages,
                max_output_tokens=first_request.max_output_tokens,
                response_format=first_request.response_format,
            )
            calls.append(first.call)
            generations.append(
                _generation_trace(
                    first,
                    phase="first",
                    messages=messages,
                    max_output_tokens=self._config.max_output_tokens,
                    response_format=first_request.response_format,
                )
            )
        except (
            ExternalRequestRejectedError,
            ExternalServiceUnavailableError,
            ValueError,
        ):
            return _refusal(
                RefusalCode.MODEL_UNAVAILABLE,
                model_calls=1,
                calls=tuple(calls),
                trace={
                    **trace_context,
                    "first_validation_code": (
                        RefusalCode.MODEL_UNAVAILABLE.value
                    ),
                    "review_triggered": False,
                    "repair_triggered": False,
                    "messages": _messages_payload(messages),
                    "response_format": first_request.response_format,
                    "max_output_tokens": self._config.max_output_tokens,
                    "generations": generations,
                },
            )
        try:
            validated = _validate_answer(
                first.content,
                evidence,
                question=question,
                calls=tuple(calls),
            )
            validation_code = _result_validation_code(validated)
            if validated.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT:
                _record_generation_validation(
                    generations,
                    "MODEL_ABSTAINED",
                )
                return self._review_abstention(
                    first_request=first_request,
                    evidence=evidence,
                    question=question,
                    answerability=answerability,
                    calls=calls,
                    generations=generations,
                    trace_context=trace_context,
                )
            _record_generation_validation(generations, validation_code)
            return replace(
                validated,
                trace={
                    **trace_context,
                    **validated.trace,
                    "first_validation_code": validation_code,
                    "review_triggered": False,
                    "repair_triggered": False,
                    "messages": _messages_payload(messages),
                    "response_format": first_request.response_format,
                    "max_output_tokens": self._config.max_output_tokens,
                    "generations": generations,
                },
            )
        except _ValidationError as first_error:
            validation_code = first_error.code
            _record_generation_validation(generations, validation_code)
        repair_request = repair_answer_request(
            first_request,
            validation_error=validation_code,
            invalid_output=first.content,
            max_output_tokens=self._config.max_repair_tokens,
        )
        repair_messages = repair_request.messages
        try:
            repaired = self._llm.generate(
                repair_messages,
                max_output_tokens=repair_request.max_output_tokens,
                response_format=repair_request.response_format,
            )
            calls.append(repaired.call)
            generations.append(
                _generation_trace(
                    repaired,
                    phase="repair",
                    messages=repair_messages,
                    max_output_tokens=self._config.max_repair_tokens,
                    response_format=repair_request.response_format,
                )
            )
            validated = _validate_answer(
                repaired.content,
                evidence,
                question=question,
                calls=tuple(calls),
            )
            repair_code = _result_validation_code(validated)
            _record_generation_validation(generations, repair_code)
            repair_trace = {
                **trace_context,
                **validated.trace,
                "first_validation_code": validation_code,
                "review_triggered": False,
                "repair_triggered": True,
                "repair_validation_code": repair_code,
                "messages": _messages_payload(messages),
                "repair_messages": _messages_payload(repair_messages),
                "response_format": repair_request.response_format,
                "max_output_tokens": self._config.max_output_tokens,
                "max_repair_tokens": self._config.max_repair_tokens,
                "generations": generations,
            }
            if validated.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT:
                return _fallback_or_refusal(
                    question,
                    evidence,
                    answerability,
                    code=RefusalCode.EVIDENCE_INSUFFICIENT,
                    model_calls=2,
                    calls=tuple(calls),
                    trace=repair_trace,
                )
            return replace(
                validated,
                trace=repair_trace,
            )
        except (
            ExternalRequestRejectedError,
            ExternalServiceUnavailableError,
        ):
            return _refusal(
                RefusalCode.VALIDATION_FAILED,
                model_calls=2,
                calls=tuple(calls),
                trace=_repair_failure_trace(
                    validation_code,
                    "MODEL_UNAVAILABLE",
                    messages,
                    repair_messages,
                    generations,
                    self._config,
                    trace_context,
                ),
            )
        except (ValueError, _ValidationError) as error:
            repair_code = (
                error.code
                if isinstance(error, _ValidationError)
                else "INVALID_MODEL_RESPONSE"
            )
            _record_generation_validation(generations, repair_code)
            return _fallback_or_refusal(
                question,
                evidence,
                answerability,
                code=RefusalCode.VALIDATION_FAILED,
                model_calls=2,
                calls=tuple(calls),
                trace=_repair_failure_trace(
                    validation_code,
                    repair_code,
                    messages,
                    repair_messages,
                    generations,
                    self._config,
                    trace_context,
                ),
            )

    def _review_abstention(  # noqa: PLR0913
        self,
        *,
        first_request: StructuredModelRequest,
        evidence: EvidenceBundle,
        question: str,
        answerability: AnswerabilityDecision,
        calls: list[ExternalCallAudit],
        generations: list[JsonValue],
        trace_context: dict[str, JsonValue],
    ) -> AnswerResult:
        """对首次空 claims 执行且只执行一次专用复核。

        Args:
            first_request: 已执行的首次回答请求。
            evidence: 首次请求使用的同一证据包。
            question: 当前原始问题。
            answerability: 首次生成前的回答性结论。
            calls: 已完成的外部调用审计记录。
            generations: 已记录的首次生成诊断。
            trace_context: 不含业务正文的回答诊断上下文。

        Returns:
            复核后安全发布的回答或稳定拒答。

        """
        review_request = abstention_review_request(
            first_request,
            max_output_tokens=self._config.max_repair_tokens,
        )
        review_messages = review_request.messages
        try:
            reviewed = self._llm.generate(
                review_messages,
                max_output_tokens=review_request.max_output_tokens,
                response_format=review_request.response_format,
            )
            calls.append(reviewed.call)
            generations.append(
                _generation_trace(
                    reviewed,
                    phase="abstention_review",
                    messages=review_messages,
                    max_output_tokens=self._config.max_repair_tokens,
                    response_format=review_request.response_format,
                )
            )
        except (
            ExternalRequestRejectedError,
            ExternalServiceUnavailableError,
            ValueError,
        ) as error:
            review_code = (
                "MODEL_UNAVAILABLE"
                if isinstance(
                    error,
                    (
                        ExternalRequestRejectedError,
                        ExternalServiceUnavailableError,
                    ),
                )
                else "INVALID_MODEL_RESPONSE"
            )
            trace = _abstention_trace(
                first_request,
                review_request,
                generations,
                trace_context,
                review_reason_code="ABSTENTION_REVIEW_INVALID",
                review_validation_code=review_code,
                config=self._config,
            )
            if isinstance(
                error,
                (
                    ExternalRequestRejectedError,
                    ExternalServiceUnavailableError,
                ),
            ):
                return _refusal(
                    RefusalCode.VALIDATION_FAILED,
                    model_calls=2,
                    calls=tuple(calls),
                    trace=trace,
                )
            return _fallback_or_refusal(
                question,
                evidence,
                answerability,
                code=RefusalCode.VALIDATION_FAILED,
                model_calls=2,
                calls=tuple(calls),
                trace=trace,
            )
        try:
            validated = _validate_answer(
                reviewed.content,
                evidence,
                question=question,
                calls=tuple(calls),
            )
        except _ValidationError as error:
            _record_generation_validation(generations, error.code)
            return _fallback_or_refusal(
                question,
                evidence,
                answerability,
                code=RefusalCode.VALIDATION_FAILED,
                model_calls=2,
                calls=tuple(calls),
                trace=_abstention_trace(
                    first_request,
                    review_request,
                    generations,
                    trace_context,
                    review_reason_code="ABSTENTION_REVIEW_INVALID",
                    review_validation_code=error.code,
                    config=self._config,
                ),
            )
        review_validation_code = _result_validation_code(validated)
        _record_generation_validation(
            generations,
            review_validation_code,
        )
        review_reason_code = (
            "ABSTENTION_REVIEW_EMPTY"
            if validated.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT
            else "ABSTENTION_REVIEW_ANSWERED"
        )
        review_trace = _abstention_trace(
            first_request,
            review_request,
            generations,
            trace_context,
            review_reason_code=review_reason_code,
            review_validation_code=review_validation_code,
            config=self._config,
        )
        if validated.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT:
            return _fallback_or_refusal(
                question,
                evidence,
                answerability,
                code=RefusalCode.EVIDENCE_INSUFFICIENT,
                model_calls=2,
                calls=tuple(calls),
                trace=review_trace,
            )
        return replace(
            validated,
            trace={**review_trace, **validated.trace},
        )

    def revision(self) -> str:
        """返回回答 prompt 与 JSON Schema 的规范化 SHA256。

        Args:
            无参数；使用当前回答协议常量。

        Returns:
            带算法前缀的规范化 SHA256。

        """
        return answer_contract_revision()


def _validate_answer(
    content: str,
    evidence: EvidenceBundle,
    *,
    question: str,
    calls: tuple[ExternalCallAudit, ...],
) -> AnswerResult:
    """校验模型回答 schema 及其全部证据约束。

    Args:
        content: LLM 返回的原始 JSON 文本。
        evidence: 本次生成允许引用的证据集合。
        question: 用于确定回答模式的当前原始问题。
        calls: 本次回答包含的外部调用审计记录。

    Returns:
        已通过逐项引用校验的回答或规范拒答。

    Raises:
        _ValidationError: JSON、回答状态、声明或引用不满足发布契约。

    """
    try:
        payload = parse_answer_response(content)
    except json.JSONDecodeError as error:
        raise _ValidationError("INVALID_JSON") from error
    except ValueError as error:
        raise _ValidationError(str(error)) from error
    raw_claims = cast(list[object], payload["claims"])
    if not raw_claims:
        return _refusal(
            RefusalCode.EVIDENCE_INSUFFICIENT,
            model_calls=len(calls),
            calls=calls,
        )
    units_by_id = {unit.unit_id: unit for unit in evidence.units}
    claims: list[AnswerClaim] = []
    claim_source_groups: list[frozenset[str]] = []
    dropped_codes: dict[str, int] = {}
    for raw_claim in raw_claims:
        try:
            claim, source_groups = _validate_claim(raw_claim, units_by_id)
            if any(existing.text == claim.text for existing in claims):
                raise _ValidationError("DUPLICATE_CLAIM")
            claims.append(claim)
            claim_source_groups.append(source_groups)
        except _ValidationError as error:
            dropped_codes[error.code] = dropped_codes.get(error.code, 0) + 1
    if not claims:
        raise _ValidationError(next(iter(dropped_codes)))
    intent = classify_question_intent(question)
    combined_source_groups: set[str] = set().union(*claim_source_groups)
    if intent in {QuestionIntent.DELIVERABLE, QuestionIntent.COMPARE} and (
        len(combined_source_groups) > 1
    ):
        mode = AnswerMode.CONFLICT
    elif dropped_codes:
        mode = AnswerMode.PARTIAL
    else:
        mode = AnswerMode.ANSWERED
    user_message = {
        AnswerMode.PARTIAL: (
            "知识库只能确认以下部分，未找到其余内容的明确依据。"
        ),
        AnswerMode.CONFLICT: (
            "不同规范存在不同表述，下面按来源分别列出。"
        ),
    }.get(mode)
    dropped_trace_codes: dict[str, JsonValue] = dict(dropped_codes)
    return AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer="\n\n".join(claim.text for claim in claims),
        claims=tuple(claims),
        refusal_code=None,
        model_calls=len(calls),
        calls=calls,
        answer_mode=mode,
        user_message=user_message,
        trace={
            "dropped_claim_count": sum(dropped_codes.values()),
            "dropped_claim_codes": dropped_trace_codes,
        },
    )


def _validate_claim(
    raw_claim: object,
    units_by_id: dict[str, EvidenceUnit],
) -> tuple[AnswerClaim, frozenset[str]]:
    """校验单条声明及其证据覆盖范围。

    Args:
        raw_claim: 模型生成的未信任声明值。
        units_by_id: 本次允许引用的原子证据映射。

    Returns:
        引用唯一、来源一致且数字受支持的声明及其来源组。

    Raises:
        _ValidationError: 声明 schema、引用或数字支持不符合契约。

    """
    claim = cast(dict[str, object], raw_claim)
    text = cast(str, claim["text"])
    support_ids = cast(list[str], claim["support_ids"])
    if len(set(support_ids)) != len(support_ids):
        raise _ValidationError("DUPLICATE_SUPPORT")
    units = tuple(_resolve_support(item, units_by_id) for item in support_ids)
    source_groups = frozenset(item.source_group for item in units)
    if len(source_groups) != 1:
        raise _ValidationError("CROSS_SOURCE_GROUP")
    if all(unit.low_confidence_ocr for unit in units):
        raise _ValidationError("LOW_CONFIDENCE_OCR_ONLY")
    support_text = "\n".join(item.text for item in units)
    if any(
        number not in support_text for number in _NUMBER_PATTERN.findall(text)
    ):
        raise _ValidationError("UNSUPPORTED_NUMBER")
    supports = tuple(
        ClaimSupport(
            evidence_id=unit.evidence_id,
            chunk_id=unit.chunk_id,
            quote=unit.text,
            locator=unit.locator.display(),
        )
        for unit in units
    )
    return AnswerClaim(text=text.strip(), supports=supports), source_groups


def _resolve_support(
    support_id: str,
    units_by_id: dict[str, EvidenceUnit],
) -> EvidenceUnit:
    unit = units_by_id.get(support_id)
    if unit is None:
        raise _ValidationError("INVALID_SUPPORT_ID")
    return unit


def _refusal(  # noqa: PLR0913
    code: RefusalCode,
    *,
    model_calls: int,
    calls: tuple[ExternalCallAudit, ...],
    answer_mode: AnswerMode = AnswerMode.INTERNAL_VALIDATION_ERROR,
    user_message: str | None = None,
    trace: dict[str, JsonValue] | None = None,
) -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.REFUSED,
        answer=None,
        claims=(),
        refusal_code=code,
        model_calls=model_calls,
        calls=calls,
        answer_mode=answer_mode,
        user_message=user_message or _refusal_message(code),
        trace={} if trace is None else trace,
    )


def _fallback_or_refusal(  # noqa: PLR0913
    question: str,
    evidence: EvidenceBundle,
    answerability: AnswerabilityDecision,
    *,
    code: RefusalCode,
    model_calls: int,
    calls: tuple[ExternalCallAudit, ...],
    trace: dict[str, JsonValue],
) -> AnswerResult:
    """仅对高置信可回答请求发布确定性原文兜底。"""
    if answerability.status is not AnswerabilityStatus.SUPPORTED:
        return _refusal(
            code,
            model_calls=model_calls,
            calls=calls,
            trace=trace,
        )
    intent = classify_question_intent(question)
    limit = 2 if intent is QuestionIntent.DEFINITION else 4
    selected = _matching_fallback_units(
        question,
        evidence,
        intent=intent,
        limit=limit,
    )
    if not selected:
        return _refusal(
            code,
            model_calls=model_calls,
            calls=calls,
            trace=trace,
        )
    claims = tuple(
        AnswerClaim(
            text=unit.text.strip(),
            supports=(
                ClaimSupport(
                    evidence_id=unit.evidence_id,
                    chunk_id=unit.chunk_id,
                    quote=unit.text,
                    locator=unit.locator.display(),
                ),
            ),
        )
        for unit in selected
        if unit.text.strip()
    )
    if not claims:
        return _refusal(
            code,
            model_calls=model_calls,
            calls=calls,
            trace=trace,
        )
    return AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer="\n\n".join(claim.text for claim in claims),
        claims=claims,
        refusal_code=None,
        model_calls=model_calls,
        calls=calls,
        answer_mode=AnswerMode.EXTRACTIVE_FALLBACK,
        user_message=None,
        trace={**trace, "extractive_fallback": True},
    )


def _matching_fallback_units(
    question: str,
    evidence: EvidenceBundle,
    *,
    intent: QuestionIntent,
    limit: int,
) -> tuple[EvidenceUnit, ...]:
    """按问题关键词和意图对 top evidence units 做确定性窄选。"""
    terms = _question_terms(question)
    ranked: list[tuple[int, int, EvidenceUnit]] = []
    for index, unit in enumerate(evidence.units):
        if unit.low_confidence_ocr:
            continue
        score = sum(term.casefold() in unit.text.casefold() for term in terms)
        if intent is QuestionIntent.PROCEDURE and any(
            marker in unit.text
            for marker in ("提交", "评估", "确认", "审批", "更新", "执行")
        ):
            score += 1
        if intent is QuestionIntent.ACTOR and any(
            marker in unit.text for marker in ("责任人", "负责人", "负责")
        ):
            score += 1
        if intent is QuestionIntent.DELIVERABLE and any(
            marker in unit.text for marker in ("输出", "报告", "文档", "《")
        ):
            score += 1
        if intent is QuestionIntent.DEFINITION and any(
            marker in unit.text for marker in ("是指", "定义", "简称", "即")
        ):
            score += 1
        if score > 0:
            ranked.append((-score, index, unit))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:limit])


def _question_terms(question: str) -> tuple[str, ...]:
    normalized = question
    for marker in (
        "知识库",
        "是否记载",
        "是什么",
        "什么是",
        "需要",
        "哪些",
        "什么",
        "如何",
        "怎么",
        "完成后",
        "包括",
        "内容",
        "步骤",
        "流程",
    ):
        normalized = normalized.replace(marker, " ")
    raw_terms = re.findall(r"[A-Za-z0-9-]+|[\u4e00-\u9fff]{2,}", normalized)
    terms: list[str] = []
    for term in raw_terms:
        if term not in terms:
            terms.append(term)
        if len(term) > _LONG_CHINESE_TERM_LENGTH and not term.isascii():
            for start in range(len(term) - 2):
                fragment = term[start : start + 3]
                if fragment not in terms:
                    terms.append(fragment)
    return tuple(terms)


def _refusal_message(code: RefusalCode) -> str:
    messages = {
        RefusalCode.NO_EVIDENCE: "知识库中暂未检索到可用资料。",
        RefusalCode.PROMPT_INJECTION: "检索资料未通过安全检查，无法生成回答。",
        RefusalCode.LOW_CONFIDENCE_OCR_ONLY: (
            "当前仅检索到低置信度 OCR 内容，暂不能作为可靠回答依据。"
        ),
        RefusalCode.EVIDENCE_INSUFFICIENT: (
            "知识库中暂未找到能够支持该问题的资料。"
        ),
        RefusalCode.MODEL_UNAVAILABLE: (
            "回答服务暂时不可用，请稍后重试并查看 Trace。"
        ),
        RefusalCode.VALIDATION_FAILED: (
            "已找到相关资料，但回答引用校验未通过，请稍后重试并查看 Trace。"
        ),
    }
    return messages[code]


def _answerability_trace(
    decision: AnswerabilityDecision,
) -> dict[str, JsonValue]:
    return {
        "answerability_decision": decision.status.value,
        "answerability_top_score": decision.top_score,
        "strong_anchor_count": decision.strong_anchor_count,
        "covered_anchor_count": decision.covered_anchor_count,
        "answerability_non_low_ocr_count": (
            decision.non_low_ocr_evidence_count
        ),
    }


def _answer_trace_context(
    request: StructuredModelRequest,
    evidence: EvidenceBundle,
) -> dict[str, JsonValue]:
    """构造 SAFE Trace 可见的非正文回答上下文。

    Args:
        request: 已包含确定性问题意图的首次请求。
        evidence: 已完成隔离的证据包。

    Returns:
        只含意图和证据计数的诊断属性。

    Raises:
        ValueError: 内部回答请求缺少确定性意图。

    """
    intent = request.user_payload.get("question_intent")
    if not isinstance(intent, str):
        raise ValueError("回答请求缺少 question_intent。")
    return {
        "intent": intent,
        "response_format": request.response_format,
        "evidence_count": len(evidence.items),
        "non_low_ocr_evidence_count": sum(
            not item.low_confidence_ocr for item in evidence.items
        ),
    }


def _generation_trace(
    generated: LlmGeneration,
    *,
    phase: str,
    messages: tuple[ChatMessage, ...],
    max_output_tokens: int,
    response_format: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """构造一次生成调用的完整诊断属性。

    Args:
        generated: 已通过客户端响应校验的生成结果。
        phase: 初次生成或修复阶段标识。
        messages: 实际发送给模型的消息。
        max_output_tokens: 本次调用使用的输出 token 上限。
        response_format: 本次调用实际使用的动态 JSON Schema。

    Returns:
        包含调用、用量、请求和原始输出的 Trace 属性。

    """
    return {
        "phase": phase,
        "model": generated.model,
        "endpoint": generated.call.endpoint,
        "retry_count": generated.call.retry_count,
        "elapsed_ms": round(generated.call.elapsed_seconds * 1000),
        "queue_ms": None,
        "ttft_ms": None,
        "prompt_tokens": generated.usage.prompt_tokens,
        "completion_tokens": generated.usage.completion_tokens,
        "total_tokens": generated.usage.total_tokens,
        "max_output_tokens": max_output_tokens,
        "completion_tokens_per_second": (
            round(
                generated.usage.completion_tokens
                / generated.call.elapsed_seconds,
                2,
            )
            if generated.call.elapsed_seconds > 0
            else None
        ),
        "messages": _messages_payload(messages),
        "response_format": response_format,
        **_response_shape(generated.content),
        "raw_output": generated.content,
    }


def _response_shape(content: str) -> dict[str, JsonValue]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {
            "claims_count": None,
            "top_level_keys": [],
            "json_parse_ok": False,
        }
    if not isinstance(payload, dict):
        return {
            "claims_count": None,
            "top_level_keys": [],
            "json_parse_ok": True,
        }
    claims = payload.get("claims")
    return {
        "claims_count": len(claims) if isinstance(claims, list) else None,
        "top_level_keys": sorted(payload),
        "json_parse_ok": True,
    }


def _record_generation_validation(
    generations: list[JsonValue],
    validation_code: str,
) -> None:
    if generations and isinstance(generations[-1], dict):
        generations[-1]["validation_code"] = validation_code


def _result_validation_code(result: AnswerResult) -> str:
    if result.refusal_code is RefusalCode.EVIDENCE_INSUFFICIENT:
        return RefusalCode.EVIDENCE_INSUFFICIENT.value
    return "VALIDATION_OK"


def _messages_payload(
    messages: tuple[ChatMessage, ...],
) -> list[JsonValue]:
    return [
        {"role": message.role, "content": message.content}
        for message in messages
    ]


def _abstention_trace(  # noqa: PLR0913
    first_request: StructuredModelRequest,
    review_request: StructuredModelRequest,
    generations: list[JsonValue],
    trace_context: dict[str, JsonValue],
    *,
    review_reason_code: str,
    review_validation_code: str,
    config: AnswerConfig,
) -> dict[str, JsonValue]:
    """构造一次空回答复核的完整诊断属性。

    Args:
        first_request: 已执行的首次请求。
        review_request: 已执行的专用复核请求。
        generations: 最多两次模型调用的诊断记录。
        trace_context: 不含业务正文的意图和证据计数。
        review_reason_code: 复核 answered、empty 或 invalid 结果码。
        review_validation_code: 复核输出的确定性校验码。
        config: 冻结的回答输出预算。

    Returns:
        可供 SAFE 属性筛选和 FULL artifact 使用的诊断对象。

    """
    return {
        **trace_context,
        "first_validation_code": "MODEL_ABSTAINED",
        "review_triggered": True,
        "review_reason_code": review_reason_code,
        "review_validation_code": review_validation_code,
        "repair_triggered": False,
        "messages": _messages_payload(first_request.messages),
        "review_messages": _messages_payload(review_request.messages),
        "response_format": first_request.response_format,
        "max_output_tokens": config.max_output_tokens,
        "max_review_tokens": config.max_repair_tokens,
        "generations": generations,
    }


def _repair_failure_trace(  # noqa: PLR0913, PLR0917
    first_code: str,
    repair_code: str,
    messages: tuple[ChatMessage, ...],
    repair_messages: tuple[ChatMessage, ...],
    generations: list[JsonValue],
    config: AnswerConfig,
    trace_context: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """构造回答修复仍失败时的完整诊断属性。

    Args:
        first_code: 初次回答的稳定校验失败码。
        repair_code: 修复回答的稳定校验失败码。
        messages: 初次生成使用的消息。
        repair_messages: 修复生成使用的消息。
        generations: 两次模型调用的诊断记录。
        config: 冻结的回答输出预算。
        trace_context: 不含业务正文的意图和证据计数。

    Returns:
        描述两次校验失败及调用上下文的 Trace 属性。

    """
    return {
        **trace_context,
        "first_validation_code": first_code,
        "review_triggered": False,
        "repair_triggered": True,
        "repair_validation_code": repair_code,
        "messages": _messages_payload(messages),
        "repair_messages": _messages_payload(repair_messages),
        "response_format": trace_context.get("response_format"),
        "max_output_tokens": config.max_output_tokens,
        "max_repair_tokens": config.max_repair_tokens,
        "generations": generations,
    }
