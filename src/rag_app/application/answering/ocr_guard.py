"""识别只应由 PP-OCRv6 二次核对的高风险答案原子。"""

from __future__ import annotations

import re

from rag_app.core.models import AnswerClaim, EvidenceItem, SourceSpanKind

_CRITICAL_PATTERNS = (
    re.compile(r"(?<![\w.])[-−]\s*\d+(?:\.\d+)?"),
    re.compile(r"(?<![\w.])\d+\.\d+(?![\w.])"),
    re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*[%％]"),
    re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:mW|MW)(?![A-Za-z])"),
    re.compile(
        r"不得|不能|不可|禁止|严禁|不允许|must\s+not|shall\s+not|may\s+not",
        re.IGNORECASE,
    ),
    re.compile(r"至少|至多|不少于|不超过|>=|<=|≥|≤"),
    re.compile(r"(?<!\d)\d{4}(?:[-/.年]\d{1,2})(?:[-/.月]\d{1,2}日?)?(?!\d)"),
    re.compile(r"(?<![A-Za-z0-9])(?:V|v)\d+(?:\.\d+)+(?![A-Za-z0-9])"),
)
_VISUAL_KINDS = frozenset(
    {SourceSpanKind.PDF_PARSED_TEXT, SourceSpanKind.OCR_TEXT}
)


def critical_ocr_atoms(text: str) -> tuple[str, ...]:
    """按原始大小写返回需二次 OCR 的去重关键原子。"""
    matches = sorted(
        (
            (match.start(), match.end(), match.group(0))
            for pattern in _CRITICAL_PATTERNS
            for match in pattern.finditer(text)
        ),
        key=lambda item: (item[0], -(item[1] - item[0])),
    )
    atoms: list[str] = []
    covered: list[tuple[int, int]] = []
    for start, end, atom in matches:
        if any(start >= left and end <= right for left, right in covered):
            continue
        if atom not in atoms:
            atoms.append(atom)
        covered.append((start, end))
    return tuple(atoms)


def claim_pdf_visual_evidence(
    claim: AnswerClaim,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[EvidenceItem, ...]:
    """返回本 Claim 真正引用且具有物理 PDF 页的视觉解析证据。"""
    support_ids = {support.support_id for support in claim.supports}
    return tuple(
        item
        for item in evidence
        if item.evidence_id in support_ids
        and any(
            span.span_type in _VISUAL_KINDS
            and span.source_anchor is not None
            and span.source_anchor.page_index is not None
            for span in item.source_spans
        )
    )


__all__ = ["claim_pdf_visual_evidence", "critical_ocr_atoms"]
