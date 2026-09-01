"""逐 claim 引文校验、空回答复核与最多一次修复。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
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
    StreamCancellation,
)
from rag_app.generation.evidence import (
    AnswerabilityDecision,
    AnswerabilityStatus,
    EvidenceBundle,
    EvidenceUnit,
    decide_answerability,
    required_question_anchors,
)
from rag_app.generation.question_intent import (
    QuestionIntent,
    classify_question_intent,
)
from rag_app.generation.question_profile import (
    QuestionProfile,
    legacy_question_profile,
)
from rag_app.generation.streaming_claims import IncrementalClaimsParser
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
_LOW_RANK_MAX = 4
_LOW_SUPPORT_SCORE_MIN = 0.2


class AnswerStatus(StrEnum):
    """回答发布状态。"""

    ANSWERED = "answered"
    REFUSED = "refused"


class AnswerMode(StrEnum):
    """面向 API 和前端的稳定回答方式。"""

    ANSWERED = "ANSWERED"
    PARTIAL = "PARTIAL"
    CONFLICT = "CONFLICT"
    SOURCE_SEPARATED = "SOURCE_SEPARATED"
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


class _StreamFinalValidationError(RuntimeError):
    """已有 claim 发布后，最终完整 JSON 未保持同一安全结果。"""


@dataclass(slots=True)
class _StreamingClaimState:
    """在完整回答结束前增量校验并发布单条 claim。"""

    units_by_id: dict[str, EvidenceUnit]
    question_anchors: tuple[str, ...]
    on_claim: Callable[[AnswerClaim], None]
    started: float
    parser: IncrementalClaimsParser = field(
        default_factory=IncrementalClaimsParser
    )
    validated_claims: list[AnswerClaim] = field(default_factory=list)
    dropped_codes: dict[str, int] = field(default_factory=dict)
    parser_error: str | None = None
    first_validated_claim_ms: int | None = None

    def consume(self, fragment: str) -> None:
        """解析并立即发布本分片中新完成且通过门禁的 claim。

        Args:
            fragment: 一个非空模型 content delta。

        Returns:
            无返回值；无效 claim 只累计非敏感错误码。

        """
        if self.parser_error is not None:
            return
        try:
            raw_claims = self.parser.feed(fragment)
        except ValueError as error:
            self.parser_error = str(error)
            return
        for raw_claim in raw_claims:
            try:
                validated_raw = _validate_streamed_claim_shape(raw_claim)
                claim, _ = _validate_claim(
                    validated_raw,
                    self.units_by_id,
                    question_anchors=self.question_anchors,
                )
                if any(
                    existing.text == claim.text
                    for existing in self.validated_claims
                ):
                    raise _ValidationError("DUPLICATE_CLAIM")
            except _ValidationError as error:
                self.dropped_codes[error.code] = (
                    self.dropped_codes.get(error.code, 0) + 1
                )
                continue
            self.validated_claims.append(claim)
            if self.first_validated_claim_ms is None:
                self.first_validated_claim_ms = max(
                    0,
                    round((time.monotonic() - self.started) * 1000),
                )
            self.on_claim(claim)

    def finish(self, content: str) -> None:
        """用完整 JSON 契约确认增量结果与最终 claims 一致。

        Args:
            content: SSE 完成后累积的完整模型字符串。

        Returns:
            无返回值。

        Raises:
            _StreamFinalValidationError: 已发布 claim 后完整结果不一致。

        """
        try:
            self.parser.finish()
            payload = parse_answer_response(content)
            final_claims = cast(list[object], payload["claims"])
        except (json.JSONDecodeError, ValueError) as error:
            self.parser_error = str(error)
            if self.validated_claims:
                raise _StreamFinalValidationError(
                    "STREAMED_ANSWER_FINAL_INVALID"
                ) from error
            return
        if tuple(final_claims) != self.parser.claims:
            self.parser_error = "STREAMED_CLAIMS_MISMATCH"
            if self.validated_claims:
                raise _StreamFinalValidationError(
                    "STREAMED_CLAIMS_MISMATCH"
                )

    def trace(self) -> dict[str, JsonValue]:
        """返回不含模型正文的增量校验指标。

        Args:
            无参数；读取当前流状态。

        Returns:
            首条校验耗时、有效/丢弃计数与解析状态。

        """
        return {
            "first_validated_claim_ms": self.first_validated_claim_ms,
            "validated_claim_count": len(self.validated_claims),
            "stream_dropped_claim_count": sum(self.dropped_codes.values()),
            "stream_parser_error": self.parser_error,
        }


class AnswerGenerator:
    """增量发布已验证 claim，并以完整契约收束最终回答。"""

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
        question_profile: QuestionProfile | None = None,
        rerank_scores: tuple[float, ...] = (),
        _on_claim: Callable[[AnswerClaim], None] | None = None,
        _cancellation: StreamCancellation | None = None,
    ) -> AnswerResult:
        """回答当前问题，未通过门禁时返回稳定拒答。

        Args:
            question: 当前原始问题。
            evidence: 已隔离注入并满足 token 预算的证据包。
            question_profile: QueryService 选择的回答组织 profile；直接调用时
                兼容 legacy profile。
            rerank_scores: 按精排顺序排列的候选分数。
            _on_claim: 仅由 ``answer_stream`` 注入的已验证 claim 回调。
            _cancellation: 仅由 ``answer_stream`` 注入的上游取消令牌。

        Returns:
            可安全发布的回答或拒答。

        """
        preflight = _answer_preflight(
            question,
            evidence,
            rerank_scores=rerank_scores,
        )
        if isinstance(preflight, AnswerResult):
            return preflight
        answerability = preflight
        active_profile = question_profile or legacy_question_profile(question)
        first_request = answer_request(
            question,
            evidence_bundle=json.loads(evidence.rendered_json),
            question_profile=active_profile,
            max_output_tokens=self._config.max_output_tokens,
        )
        trace_context = {
            **_answer_trace_context(first_request, evidence),
            **_answerability_trace(answerability),
        }
        messages = first_request.messages
        calls: list[ExternalCallAudit] = []
        generations: list[JsonValue] = []
        stream_state = _new_streaming_claim_state(
            question,
            evidence,
            on_claim=_on_claim,
            cancellation=_cancellation,
        )
        try:
            first = self._generate_first_answer(
                request=first_request,
                stream_state=stream_state,
                cancellation=_cancellation,
                calls=calls,
                generations=generations,
                trace_context=trace_context,
            )
        except (
            ExternalRequestRejectedError,
            ExternalServiceUnavailableError,
            ValueError,
        ) as error:
            if stream_state is not None and stream_state.validated_claims:
                raise _StreamFinalValidationError(
                    "STREAMED_ANSWER_FINAL_INVALID"
                ) from error
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
                question,
                first.content,
                evidence,
                calls=tuple(calls),
            )
            validation_code = _result_validation_code(validated)
            if (
                stream_state is not None
                and stream_state.validated_claims
                and tuple(stream_state.validated_claims) != validated.claims
            ):
                raise _StreamFinalValidationError(
                    "STREAMED_CLAIMS_VALIDATION_MISMATCH"
                )
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
                    **(
                        {}
                        if stream_state is None
                        else stream_state.trace()
                    ),
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
            if stream_state is not None and stream_state.validated_claims:
                raise _StreamFinalValidationError(
                    "STREAMED_ANSWER_FINAL_INVALID"
                ) from first_error
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
                question,
                repaired.content,
                evidence,
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

    def _generate_first_answer(  # noqa: PLR0913
        self,
        *,
        request: StructuredModelRequest,
        stream_state: _StreamingClaimState | None,
        cancellation: StreamCancellation | None,
        calls: list[ExternalCallAudit],
        generations: list[JsonValue],
        trace_context: dict[str, JsonValue],
    ) -> LlmGeneration:
        """执行首次缓冲或流式生成并记录非敏感调用指标。"""
        first = (
            self._llm.generate(
                request.messages,
                max_output_tokens=request.max_output_tokens,
                response_format=request.response_format,
            )
            if stream_state is None
            else self._llm.generate_stream(
                request.messages,
                max_output_tokens=request.max_output_tokens,
                response_format=request.response_format,
                on_delta=stream_state.consume,
                cancellation=cast(StreamCancellation, cancellation),
            )
        )
        calls.append(first.call)
        generations.append(
            _generation_trace(
                first,
                phase="first",
                messages=request.messages,
                max_output_tokens=self._config.max_output_tokens,
                response_format=request.response_format,
            )
        )
        if stream_state is not None:
            stream_state.finish(first.content)
            trace_context.update(stream_state.trace())
        if first.stream is not None:
            trace_context.update(_stream_trace(first))
        return first

    def answer_stream(  # noqa: PLR0913
        self,
        question: str,
        evidence: EvidenceBundle,
        *,
        on_claim: Callable[[AnswerClaim], None],
        cancellation: StreamCancellation,
        question_profile: QuestionProfile | None = None,
        rerank_scores: tuple[float, ...] = (),
    ) -> AnswerResult:
        """流式生成首次回答并只回调已经通过全部门禁的 claim。

        Args:
            question: 当前原始问题。
            evidence: 与非流式回答相同的原子证据包。
            on_claim: 按模型顺序接收可立即展示的已验证 claim。
            cancellation: 客户端断开时关闭上游 SSE 的令牌。
            question_profile: 与非流式路径完全相同的回答组织 profile。
            rerank_scores: 按精排顺序排列的候选分数。

        Returns:
            与 ``answer`` 完全相同的 canonical 最终结果。

        """
        return self.answer(
            question,
            evidence,
            question_profile=question_profile,
            rerank_scores=rerank_scores,
            _on_claim=on_claim,
            _cancellation=cancellation,
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
                question,
                reviewed.content,
                evidence,
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


def _new_streaming_claim_state(
    question: str,
    evidence: EvidenceBundle,
    *,
    on_claim: Callable[[AnswerClaim], None] | None,
    cancellation: StreamCancellation | None,
) -> _StreamingClaimState | None:
    """为首次流式回答创建增量门禁状态。"""
    if on_claim is None:
        return None
    if cancellation is None:
        raise ValueError("流式回答必须提供 cancellation。")
    return _StreamingClaimState(
        units_by_id={unit.unit_id: unit for unit in evidence.units},
        question_anchors=required_question_anchors(question),
        on_claim=on_claim,
        started=time.monotonic(),
    )


def _answer_preflight(
    question: str,
    evidence: EvidenceBundle,
    *,
    rerank_scores: tuple[float, ...],
) -> AnswerabilityDecision | AnswerResult:
    """执行不调用模型的基础证据与可回答性门禁。"""
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
    if answerability.status is not AnswerabilityStatus.NOT_FOUND:
        return answerability
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


def _stream_trace(generation: LlmGeneration) -> dict[str, JsonValue]:
    """提取不含正文的首次 SSE 调用指标。"""
    stream = generation.stream
    if stream is None:
        return {}
    return {
        "llm_stream": True,
        "selected_endpoint": generation.call.endpoint,
        "first_delta_ms": (
            None
            if stream.first_delta_seconds is None
            else round(stream.first_delta_seconds * 1000)
        ),
        "delta_count": stream.delta_count,
        "stream_cancelled": False,
        "stream_finish_reason": stream.finish_reason,
        "retry_count": generation.call.retry_count,
    }


def _validate_answer(
    question: str,
    content: str,
    evidence: EvidenceBundle,
    *,
    calls: tuple[ExternalCallAudit, ...],
) -> AnswerResult:
    """校验模型回答 schema 及其全部证据约束。

    Args:
        question: 当前原始问题。
        content: LLM 返回的原始 JSON 文本。
        evidence: 本次生成允许引用的证据集合。
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
    question_anchors = required_question_anchors(question)
    claims: list[AnswerClaim] = []
    claim_source_groups: list[frozenset[str]] = []
    selected_units: list[EvidenceUnit] = []
    dropped_codes: dict[str, int] = {}
    for raw_claim in raw_claims:
        try:
            claim, source_groups = _validate_claim(
                raw_claim,
                units_by_id,
                question_anchors=question_anchors,
            )
            if any(existing.text == claim.text for existing in claims):
                raise _ValidationError("DUPLICATE_CLAIM")
            claims.append(claim)
            claim_source_groups.append(source_groups)
            selected_units.extend(
                _claim_evidence_units(raw_claim, units_by_id)
            )
        except _ValidationError as error:
            dropped_codes[error.code] = dropped_codes.get(error.code, 0) + 1
    if not claims:
        raise _ValidationError(next(iter(dropped_codes)))
    if question_anchors and not _units_cover_question_anchors(
        tuple(selected_units),
        question_anchors,
    ):
        raise _ValidationError("UNSUPPORTED_QUESTION_ANCHOR")
    combined_source_groups: set[str] = set().union(*claim_source_groups)
    if len(combined_source_groups) > 1:
        mode = AnswerMode.SOURCE_SEPARATED
    elif dropped_codes:
        mode = AnswerMode.PARTIAL
    else:
        mode = AnswerMode.ANSWERED
    user_message = {
        AnswerMode.PARTIAL: (
            "知识库只能确认以下部分，未找到其余内容的明确依据。"
        ),
        AnswerMode.SOURCE_SEPARATED: (
            "下面按模式或来源分别列出。"
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
            **_selected_support_quality(selected_units),
        },
    )


def _validate_streamed_claim_shape(
    raw_claim: dict[str, object],
) -> object:
    """用现有完整契约校验一个刚闭合的 claim 外形。

    Args:
        raw_claim: 增量解析器通过标准 ``json.loads`` 得到的对象。

    Returns:
        与最终解析路径相同的规范 claim 对象。

    Raises:
        _ValidationError: claim 外形不满足当前 answer schema。

    """
    try:
        payload = parse_answer_response(
            json.dumps(
                {"claims": [raw_claim]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise _ValidationError("INVALID_CLAIM_SCHEMA") from error
    claims = cast(list[object], payload["claims"])
    if len(claims) != 1:
        raise _ValidationError("INVALID_CLAIM_SCHEMA")
    return claims[0]


def _validate_claim(
    raw_claim: object,
    units_by_id: dict[str, EvidenceUnit],
    *,
    question_anchors: tuple[str, ...] = (),
) -> tuple[AnswerClaim, frozenset[str]]:
    """校验单条声明及其证据覆盖范围。

    Args:
        raw_claim: 模型生成的未信任声明值。
        units_by_id: 本次允许引用的原子证据映射。
        question_anchors: 问题中必须被所选证据直接支持的显式主体。

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
    if question_anchors and not _units_support_question_anchor(
        units,
        question_anchors,
    ):
        raise _ValidationError("UNSUPPORTED_QUESTION_ANCHOR")
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


def _claim_evidence_units(
    raw_claim: object,
    units_by_id: dict[str, EvidenceUnit],
) -> tuple[EvidenceUnit, ...]:
    """确定性恢复一条已验证 claim 的最终证据单元。"""
    claim = cast(dict[str, object], raw_claim)
    support_ids = cast(list[str], claim["support_ids"])
    return tuple(
        _resolve_support(support_id, units_by_id)
        for support_id in support_ids
    )


def _selected_support_quality(
    selected_units: list[EvidenceUnit],
) -> dict[str, JsonValue]:
    """生成只用于 SAFE Trace 的最终支持质量诊断。"""
    ranks: list[JsonValue] = [unit.rerank_rank for unit in selected_units]
    scores = [unit.rerank_score for unit in selected_units]
    return {
        "selected_support_ranks": ranks,
        "min_selected_support_score": min(scores) if scores else None,
        "low_rank_support_count": sum(
            unit.rerank_rank > _LOW_RANK_MAX
            or score < _LOW_SUPPORT_SCORE_MIN
            for unit, score in zip(selected_units, scores, strict=True)
        ),
    }


def _units_support_question_anchor(
    units: tuple[EvidenceUnit, ...],
    question_anchors: tuple[str, ...],
) -> bool:
    """检查所选证据正文或来源标签是否直接包含问题显式主体。"""
    searchable = "\n".join(
        f"{unit.source_label}\n{unit.text}".casefold() for unit in units
    )
    return any(anchor.casefold() in searchable for anchor in question_anchors)


def _units_cover_question_anchors(
    units: tuple[EvidenceUnit, ...],
    question_anchors: tuple[str, ...],
) -> bool:
    """检查最终证据是否合计覆盖全部显式主体。"""
    searchable = "\n".join(
        f"{unit.source_label}\n{unit.text}".casefold() for unit in units
    )
    return all(anchor.casefold() in searchable for anchor in question_anchors)


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
    question_anchors = required_question_anchors(question)
    if question_anchors and not _units_cover_question_anchors(
        selected,
        question_anchors,
    ):
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
    question_anchors = required_question_anchors(question)
    ranked: list[tuple[int, int, EvidenceUnit]] = []
    for index, unit in enumerate(evidence.units):
        if unit.low_confidence_ocr:
            continue
        if question_anchors and not _units_support_question_anchor(
            (unit,),
            question_anchors,
        ):
            continue
        searchable = (
            f"{unit.source_label}\n{unit.text}"
            if question_anchors
            else unit.text
        ).casefold()
        score = sum(term.casefold() in searchable for term in terms)
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
    if len(question_anchors) <= 1:
        return tuple(item[2] for item in ranked[:limit])
    selected: list[EvidenceUnit] = []
    for anchor in question_anchors:
        match = next(
            (
                item[2]
                for item in ranked
                if item[2] not in selected
                and _units_support_question_anchor((item[2],), (anchor,))
            ),
            None,
        )
        if match is not None:
            selected.append(match)
    selected.extend(item[2] for item in ranked if item[2] not in selected)
    return tuple(selected[:limit])


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
    profile = request.user_payload.get("question_profile")
    if not isinstance(profile, dict):
        raise ValueError("回答请求缺少 question_profile。")
    intent = profile.get("primary_operation")
    if not isinstance(intent, str):
        raise ValueError("回答请求缺少 primary_operation。")
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
    stream = generated.stream
    return {
        "phase": phase,
        "model": generated.model,
        "endpoint": generated.call.endpoint,
        "selected_endpoint": generated.call.endpoint,
        "retry_count": generated.call.retry_count,
        "elapsed_ms": round(generated.call.elapsed_seconds * 1000),
        "queue_ms": None,
        "ttft_ms": (
            None
            if stream is None or stream.first_delta_seconds is None
            else round(stream.first_delta_seconds * 1000)
        ),
        "llm_stream": stream is not None,
        "first_delta_ms": (
            None
            if stream is None or stream.first_delta_seconds is None
            else round(stream.first_delta_seconds * 1000)
        ),
        "delta_count": 0 if stream is None else stream.delta_count,
        "stream_cancelled": False,
        "stream_finish_reason": (
            None if stream is None else stream.finish_reason
        ),
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
