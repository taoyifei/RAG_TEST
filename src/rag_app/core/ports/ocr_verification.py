"""回答发布前高风险视觉文字复核端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models import (
    AnswerClaim,
    EvidenceItem,
    OcrClaimVerification,
)


class CriticalOcrVerifierPort(Protocol):
    """只对最终高风险 Claim 的有限 PDF 区域执行复核。"""

    def verify(
        self,
        claim: AnswerClaim,
        evidence: tuple[EvidenceItem, ...],
        critical_atoms: tuple[str, ...],
    ) -> OcrClaimVerification:
        """返回 PP-OCRv6 与解析结果的关键原子一致性。

        Args:
            claim: 已通过逐字来源校验的最终事实。
            evidence: 此事实实际引用的 PDF 视觉解析证据。
            critical_atoms: 需保持大小写和符号的高风险原子。

        Returns:
            VERIFIED、CONFLICT 或 UNVERIFIED 及脱敏调用记录。

        """
        ...


__all__ = ["CriticalOcrVerifierPort"]
