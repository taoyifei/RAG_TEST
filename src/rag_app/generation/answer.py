"""逐 claim 精确引文校验与最多一次修复。"""

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
from rag_app.generation.evidence import EvidenceBundle, EvidenceItem
from rag_app.model_contracts import (
    answer_contract_revision,
    answer_request,
    answer_response_format,
    parse_answer_response,
    repair_answer_request,
)
from rag_app.tracing.models import JsonValue

__all__ = [
    "AnswerClaim",
    "AnswerConfig",
    "AnswerGenerator",
    "AnswerResult",
    "AnswerStatus",
    "ClaimSupport",
    "RefusalCode",
]

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?%?")


class AnswerStatus(StrEnum):
    """回答发布状态。"""

    ANSWERED = "answered"
    REFUSED = "refused"


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
    """缓冲生成、确定性验证、一次修复后才发布。"""

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
    ) -> AnswerResult:
        """回答当前问题，未通过门禁时返回稳定拒答。

        Args:
            question: 当前原始问题。
            evidence: 已隔离注入并满足 token 预算的证据包。

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
        first_request = answer_request(
            question,
            evidence_bundle=json.loads(evidence.rendered_json),
            max_output_tokens=self._config.max_output_tokens,
        )
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
                    "first_validation_code": (
                        RefusalCode.MODEL_UNAVAILABLE.value
                    ),
                    "repair_triggered": False,
                    "messages": _messages_payload(messages),
                    "response_format": answer_response_format(),
                    "max_output_tokens": self._config.max_output_tokens,
                    "generations": generations,
                },
            )
        try:
            validated = _validate_answer(
                first.content,
                evidence,
                calls=tuple(calls),
            )
            validation_code = _result_validation_code(validated)
            _record_generation_validation(generations, validation_code)
            return replace(
                validated,
                trace={
                    "first_validation_code": validation_code,
                    "repair_triggered": False,
                    "messages": _messages_payload(messages),
                    "response_format": answer_response_format(),
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
                )
            )
            validated = _validate_answer(
                repaired.content,
                evidence,
                calls=tuple(calls),
            )
            repair_code = _result_validation_code(validated)
            _record_generation_validation(generations, repair_code)
            return replace(
                validated,
                trace={
                    "first_validation_code": validation_code,
                    "repair_triggered": True,
                    "repair_validation_code": repair_code,
                    "messages": _messages_payload(messages),
                    "repair_messages": _messages_payload(repair_messages),
                    "response_format": answer_response_format(),
                    "max_output_tokens": self._config.max_output_tokens,
                    "max_repair_tokens": self._config.max_repair_tokens,
                    "generations": generations,
                },
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
                ),
            )
        except (ValueError, _ValidationError) as error:
            repair_code = (
                error.code
                if isinstance(error, _ValidationError)
                else "INVALID_MODEL_RESPONSE"
            )
            _record_generation_validation(generations, repair_code)
            return _refusal(
                RefusalCode.VALIDATION_FAILED,
                model_calls=2,
                calls=tuple(calls),
                trace=_repair_failure_trace(
                    validation_code,
                    repair_code,
                    messages,
                    repair_messages,
                    generations,
                    self._config,
                ),
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
    calls: tuple[ExternalCallAudit, ...],
) -> AnswerResult:
    """校验模型回答 schema 及其全部证据约束。

    Args:
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
    evidence_by_id = {item.evidence_id: item for item in evidence.items}
    claims = tuple(
        _validate_claim(raw_claim, evidence_by_id) for raw_claim in raw_claims
    )
    if len({claim.text for claim in claims}) != len(claims):
        raise _ValidationError("DUPLICATE_CLAIM")
    return AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer="\n\n".join(claim.text for claim in claims),
        claims=claims,
        refusal_code=None,
        model_calls=len(calls),
        calls=calls,
    )


def _validate_claim(
    raw_claim: object,
    evidence_by_id: dict[str, EvidenceItem],
) -> AnswerClaim:
    """校验单条声明及其证据覆盖范围。

    Args:
        raw_claim: 模型生成的未信任声明值。
        evidence_by_id: 本次允许引用的证据映射。

    Returns:
        引用唯一、包含安全证据且数字受支持的声明。

    Raises:
        _ValidationError: 声明 schema、引用或数字支持不符合契约。

    """
    claim = cast(dict[str, object], raw_claim)
    text = cast(str, claim["text"])
    raw_supports = cast(list[object], claim["supports"])
    supports = tuple(
        _validate_support(raw_support, evidence_by_id)
        for raw_support in raw_supports
    )
    if len({(item.evidence_id, item.quote) for item in supports}) != len(
        supports
    ):
        raise _ValidationError("DUPLICATE_SUPPORT")
    safe_evidence_ids = {
        item.evidence_id
        for item in evidence_by_id.values()
        if not item.low_confidence_ocr
    }
    if not any(
        support.evidence_id in safe_evidence_ids for support in supports
    ):
        raise _ValidationError("LOW_CONFIDENCE_OCR_ONLY")
    support_text = "\n".join(item.quote for item in supports)
    if any(
        number not in support_text for number in _NUMBER_PATTERN.findall(text)
    ):
        raise _ValidationError("UNSUPPORTED_NUMBER")
    return AnswerClaim(text=text.strip(), supports=supports)


def _validate_support(
    raw_support: object,
    evidence_by_id: dict[str, EvidenceItem],
) -> ClaimSupport:
    """把单条模型引用绑定到真实证据与唯一定位。

    Args:
        raw_support: 模型生成的未信任引用值。
        evidence_by_id: 本次允许引用的证据映射。

    Returns:
        已验证逐字引文及其来源定位。

    Raises:
        _ValidationError: 引用 schema、证据 ID、原文或定位无效。

    """
    support = cast(dict[str, object], raw_support)
    evidence_id = cast(str, support["evidence_id"])
    quote = cast(str, support["quote"])
    item = evidence_by_id.get(evidence_id)
    if item is None:
        raise _ValidationError("INVALID_CITATION_ID")
    stripped_quote = quote.strip()
    if not stripped_quote or stripped_quote not in item.text:
        raise _ValidationError("QUOTE_NOT_IN_EVIDENCE")
    locator = _quote_locator(item, stripped_quote)
    return ClaimSupport(
        evidence_id=evidence_id,
        chunk_id=item.chunk_id,
        quote=stripped_quote,
        locator=locator,
    )


def _quote_locator(item: EvidenceItem, quote: str) -> str:
    """把全部 quote 出现位置唯一映射到同一个来源 locator。

    Args:
        item: 已校验 source spans 的证据项。
        quote: 已确认存在于证据原文的非空逐字引文。

    Returns:
        唯一包含全部出现位置的 locator 展示文本。

    Raises:
        _ValidationError: 引文跨 span 或映射到不同 locator。

    """
    occurrence_starts: list[int] = []
    search_start = 0
    while True:
        occurrence_start = item.text.find(quote, search_start)
        if occurrence_start < 0:
            break
        occurrence_starts.append(occurrence_start)
        search_start = occurrence_start + 1
    locators = []
    quote_length = len(quote)
    for occurrence_start in occurrence_starts:
        occurrence_end = occurrence_start + quote_length
        containing = tuple(
            span
            for span in item.source_spans
            if span.start_char <= occurrence_start
            and occurrence_end <= span.end_char
        )
        if len(containing) != 1:
            raise _ValidationError("QUOTE_CROSSES_SOURCE_SPAN")
        locator = containing[0].locator
        if locator not in locators:
            locators.append(locator)
    if len(locators) != 1:
        raise _ValidationError("AMBIGUOUS_QUOTE_LOCATION")
    return locators[0].display()


def _refusal(
    code: RefusalCode,
    *,
    model_calls: int,
    calls: tuple[ExternalCallAudit, ...],
    trace: dict[str, JsonValue] | None = None,
) -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.REFUSED,
        answer=None,
        claims=(),
        refusal_code=code,
        model_calls=model_calls,
        calls=calls,
        trace={} if trace is None else trace,
    )


def _generation_trace(
    generated: LlmGeneration,
    *,
    phase: str,
    messages: tuple[ChatMessage, ...],
    max_output_tokens: int,
) -> dict[str, JsonValue]:
    """构造一次生成调用的完整诊断属性。

    Args:
        generated: 已通过客户端响应校验的生成结果。
        phase: 初次生成或修复阶段标识。
        messages: 实际发送给模型的消息。
        max_output_tokens: 本次调用使用的输出 token 上限。

    Returns:
        包含调用、用量、请求和原始输出的 Trace 属性。

    """
    return {
        "phase": phase,
        "model": generated.model,
        "endpoint": generated.call.endpoint,
        "retry_count": generated.call.retry_count,
        "elapsed_ms": round(generated.call.elapsed_seconds * 1000),
        "prompt_tokens": generated.usage.prompt_tokens,
        "completion_tokens": generated.usage.completion_tokens,
        "total_tokens": generated.usage.total_tokens,
        "max_output_tokens": max_output_tokens,
        "messages": _messages_payload(messages),
        "response_format": answer_response_format(),
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


def _repair_failure_trace(  # noqa: PLR0913, PLR0917
    first_code: str,
    repair_code: str,
    messages: tuple[ChatMessage, ...],
    repair_messages: tuple[ChatMessage, ...],
    generations: list[JsonValue],
    config: AnswerConfig,
) -> dict[str, JsonValue]:
    """构造回答修复仍失败时的完整诊断属性。

    Args:
        first_code: 初次回答的稳定校验失败码。
        repair_code: 修复回答的稳定校验失败码。
        messages: 初次生成使用的消息。
        repair_messages: 修复生成使用的消息。
        generations: 两次模型调用的诊断记录。
        config: 冻结的回答输出预算。

    Returns:
        描述两次校验失败及调用上下文的 Trace 属性。

    """
    return {
        "first_validation_code": first_code,
        "repair_triggered": True,
        "repair_validation_code": repair_code,
        "messages": _messages_payload(messages),
        "repair_messages": _messages_payload(repair_messages),
        "response_format": answer_response_format(),
        "max_output_tokens": config.max_output_tokens,
        "max_repair_tokens": config.max_repair_tokens,
        "generations": generations,
    }
