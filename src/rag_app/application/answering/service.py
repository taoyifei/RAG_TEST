"""无证据不调用 Generator 的 P07 AnsweringService。"""

from __future__ import annotations

from rag_app.application.answering.validation import validate_extractive_draft
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
)
from rag_app.core.ports import GenerationRequest, GeneratorPort


class ExtractiveAnsweringService:
    """只在 ANSWERABLE 时调用受控 extractive Generator。"""

    def __init__(self, generator: GeneratorPort) -> None:
        self._generator = generator

    def answer(
        self,
        query: str,
        evidence: tuple[EvidenceItem, ...],
        confidence: ConfidenceDecision,
    ) -> str | None:
        """生成并验证纯 Evidence 原文回答。

        Args:
            query: 原始用户查询。
            evidence: 已通过 source-span 预算的证据。
            confidence: 决定是否允许调用 Generator 的结果。

        Returns:
            通过引用校验的 extractive 回答，拒答时为 None。

        """
        if confidence.status is not ConfidenceStatus.ANSWERABLE or not evidence:
            return None
        draft = self._generator.generate(
            GenerationRequest(
                query=query,
                evidence=evidence,
                citation_protocol="support-id-v1-extractive",
            )
        )
        validate_extractive_draft(draft, evidence)
        return draft.text


__all__ = ["ExtractiveAnsweringService"]
