"""低置信语义路由的关闭式结构化 LLM 兜底。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rag_app.clients.llm import BufferedLlmClient, ChatMessage
from rag_app.clients.model_services import ExternalCallAudit
from rag_app.generation.question_profile import (
    PrimaryOperation,
    QuestionProfile,
    RequestedSlot,
    RouteSource,
    StructuralSignals,
)
from rag_app.tracing.models import JsonValue

__all__ = ["IntentClassifier", "IntentClassifierResult"]

_SYSTEM_PROMPT = """你只负责选择问题组织方式，不回答业务问题。
只能使用给定 operation 和 slots，不能推断没有提供的事实。
只输出符合 JSON Schema 的对象，不输出 explanation、证据、文档正文或向量。"""
_OPERATION_DEFINITIONS = {
    PrimaryOperation.DEFINITION: "概念、术语或模式的含义与边界",
    PrimaryOperation.PROCEDURE: "按顺序推进或完成事项的步骤",
    PrimaryOperation.LIST: "逐项列出材料、要求、职责或内容",
    PrimaryOperation.COMPARE: "多个模式或来源的对照与差异",
    PrimaryOperation.DECISION: "基于条件判断是否需要或如何选择",
    PrimaryOperation.EXPLANATION: "原文明确的原因、目的或作用",
    PrimaryOperation.GENERAL: "没有足够把握时的中性证据回答",
}
_MAX_OUTPUT_TOKENS = 96
_MIN_CONFIDENT_SCORE = 0.5
_HIGH_SCORE_BUCKET = 0.8
_RESPONSE_FORMAT: dict[str, JsonValue] = {
    "type": "json_schema",
    "json_schema": {
        "name": "intent_classifier",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "primary_operation": {
                    "type": "string",
                    "enum": [operation.value for operation in PrimaryOperation],
                },
                "secondary_operations": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {
                        "type": "string",
                        "enum": [
                            operation.value
                            for operation in PrimaryOperation
                            if operation is not PrimaryOperation.GENERAL
                        ],
                    },
                },
                "requested_slots": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [slot.value for slot in RequestedSlot],
                    },
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "primary_operation",
                "secondary_operations",
                "requested_slots",
                "confidence",
            ],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True, slots=True)
class IntentClassifierResult:
    """一次 LLM 兜底的 profile 与脱敏调用结果。"""

    profile: QuestionProfile
    call: ExternalCallAudit | None


class IntentClassifier:
    """只在 hybrid 不确定场景中使用的有界 JSON 分类器。"""

    def __init__(
        self, llm: BufferedLlmClient, *, max_output_tokens: int
    ) -> None:
        """保存复用现有端点池的 LLM 客户端。

        Args:
            llm: 使用现有失败转移策略的 Qwen 客户端。
            max_output_tokens: 分类输出硬上限，不能超过 96。

        Returns:
            无返回值。

        Raises:
            ValueError: 输出预算不在允许范围内。

        """
        if not 1 <= max_output_tokens <= _MAX_OUTPUT_TOKENS:
            raise ValueError("intent classifier 输出预算必须在 [1, 96] 内。")
        self._llm = llm
        self._max_output_tokens = max_output_tokens

    def classify(
        self,
        resolved_query: str,
        *,
        semantic_profile: QuestionProfile,
        structural_signals: StructuralSignals,
    ) -> IntentClassifierResult:
        """对不确定语义结果执行一次失败关闭的结构化分类。

        Args:
            resolved_query: 已独立化的问题，不含证据或历史答案。
            semantic_profile: 当前 top-3 分数已分桶的语义结果。
            structural_signals: 只含高精度 slots 和二选一标记的结构信号。

        Returns:
            成功时返回 LLM profile；任何服务或 schema 失败均返回 GENERAL。

        """
        if not resolved_query.strip():
            return IntentClassifierResult(
                profile=_general(structural_signals, "EMPTY_RESOLVED_QUERY"),
                call=None,
            )
        try:
            generated = self._llm.generate(
                (
                    ChatMessage(role="system", content=_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            _request_payload(
                                resolved_query,
                                semantic_profile,
                                structural_signals,
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                ),
                max_output_tokens=self._max_output_tokens,
                response_format=_RESPONSE_FORMAT,
            )
            return IntentClassifierResult(
                profile=_parse_profile(
                    generated.content,
                    structural_signals,
                ),
                call=generated.call,
            )
        except (TypeError, ValueError, RuntimeError):
            return IntentClassifierResult(
                profile=_general(
                    structural_signals, "LLM_FALLBACK_UNAVAILABLE"
                ),
                call=None,
            )


def _request_payload(
    resolved_query: str,
    semantic_profile: QuestionProfile,
    structural_signals: StructuralSignals,
) -> dict[str, JsonValue]:
    return {
        "resolved_query": resolved_query,
        "operations": [
            {
                "operation": operation.value,
                "definition": _OPERATION_DEFINITIONS[operation],
            }
            for operation in PrimaryOperation
        ],
        "semantic_top3": [
            {"operation": operation.value, "score_bucket": _bucket(score)}
            for operation, score in semantic_profile.scores[:3]
        ],
        "requested_slots": [
            slot.value for slot in structural_signals.requested_slots
        ],
        "binary_choice_candidate": structural_signals.binary_choice_candidate,
    }


def _parse_profile(
    content: str,
    structural_signals: StructuralSignals,
) -> QuestionProfile:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("intent classifier JSON 无效。") from error
    if not isinstance(payload, dict) or set(payload) != {
        "primary_operation",
        "secondary_operations",
        "requested_slots",
        "confidence",
    }:
        raise ValueError("intent classifier schema 无效。")
    primary = PrimaryOperation(str(payload["primary_operation"]))
    secondary = tuple(
        PrimaryOperation(str(item))
        for item in _string_list(payload["secondary_operations"])
    )
    slots = tuple(
        RequestedSlot(str(item))
        for item in _string_list(payload["requested_slots"])
    )
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("intent classifier confidence 无效。")
    if float(confidence) < _MIN_CONFIDENT_SCORE:
        raise ValueError("intent classifier 置信度不足。")
    return QuestionProfile(
        primary_operation=primary,
        secondary_operations=secondary,
        requested_slots=tuple(
            slot
            for slot in RequestedSlot
            if slot in (*structural_signals.requested_slots, *slots)
        ),
        confidence=float(confidence),
        margin=0.0,
        route_source=RouteSource.LLM,
        scores=(),
        fallback_used=True,
        reason_code="LLM_FALLBACK_CONFIDENT",
    )


def _general(
    structural_signals: StructuralSignals,
    reason_code: str,
) -> QuestionProfile:
    return QuestionProfile(
        primary_operation=PrimaryOperation.GENERAL,
        secondary_operations=(),
        requested_slots=structural_signals.requested_slots,
        confidence=0.0,
        margin=0.0,
        route_source=RouteSource.GENERAL,
        scores=(),
        fallback_used=True,
        reason_code=reason_code,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError("intent classifier 数组字段无效。")
    return value


def _bucket(score: float) -> str:
    if score >= _HIGH_SCORE_BUCKET:
        return "high"
    if score >= _MIN_CONFIDENT_SCORE:
        return "medium"
    return "low"
