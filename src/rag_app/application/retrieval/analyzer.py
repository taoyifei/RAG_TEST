"""确定性、中英混合且保留关键字面信号的 QueryAnalyzer。"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import QueryAnalysis, SearchRequest

_QUOTED = re.compile(r'["“](.+?)["”]')
_IDENTIFIER = re.compile(
    r"(?<!\w)(?=[A-Za-z0-9_.\-/]{3,80}(?!\w))"
    r"(?=[A-Za-z0-9_.\-/]*[A-Za-z])"
    r"(?=[A-Za-z0-9_.\-/]*\d)"
    r"[A-Za-z0-9]+(?:[_.\-/][A-Za-z0-9]+)+(?!\w)"
)
_STANDARD = re.compile(
    r"(?<!\w)[A-Za-z]{1,8}(?:\s*/\s*[A-Za-z]{1,8})?\s*\d{2,}"
    r"(?:[-:.]\d+)*(?!\w)"
)
_NUMBER = re.compile(r"(?<!\w)-?\d+(?:\.\d+)?(?:%|％)?")
_DATE_VERSION = re.compile(
    r"\b(?:v\d+(?:\.\d+)+|\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)\b",
    flags=re.IGNORECASE,
)
_UNITS = re.compile(
    r"(?<!\w)(?:kg|g|mg|km|cm|mm|m|l|ml|kwh|kw|w|hz|mpa|pa|°c|℃|元|万元|%|％)(?!\w)",
    flags=re.IGNORECASE,
)
_TABLE_TERMS = (
    "表格",
    "哪一列",
    "第几行",
    "列值",
    "合计",
    "table",
    "column",
    "row",
)
_NEGATIONS = (
    "不",
    "未",
    "禁止",
    "不得",
    "没有",
    "并非",
    "not",
    "without",
    "never",
)
_PHONE_DIGIT_COUNT = 7


class QueryAnalyzer:
    """把不可信原始查询转换为有限且可解释的检索信号。"""

    def analyze(self, request: SearchRequest) -> QueryAnalysis:
        """执行 NFKC、信号提取和有限会话指纹。

        Args:
            request: scope、原始 query、过滤器和有限会话。

        Returns:
            保留原文且不含领域同义词黑箱的分析结果。

        """
        normalized = " ".join(
            unicodedata.normalize("NFKC", request.text).strip().split()
        )
        folded = normalized.casefold()
        identifiers = tuple(
            dict.fromkeys(
                match.group(0).strip()
                for pattern in (_IDENTIFIER, _STANDARD)
                for match in pattern.finditer(normalized)
                if not _looks_like_phone(match.group(0))
            )
        )
        quoted = tuple(
            dict.fromkeys(
                match.group(1).strip()
                for match in _QUOTED.finditer(normalized)
                if match.group(1).strip()
            )
        )
        numbers = tuple(dict.fromkeys(_NUMBER.findall(normalized)))
        units = tuple(dict.fromkeys(_UNITS.findall(normalized)))
        dates = tuple(dict.fromkeys(_DATE_VERSION.findall(normalized)))
        structural = tuple(term for term in _TABLE_TERMS if term in folded)
        negations = tuple(term for term in _NEGATIONS if term in folded)
        language: list[str] = []
        if any("\u3400" <= char <= "\u9fff" for char in normalized):
            language.append("zh")
        if re.search(r"[A-Za-z]", normalized):
            language.append("en")
        reason_codes = ["QUERY_NFKC_NORMALIZED"]
        if identifiers:
            reason_codes.append("IDENTIFIER_SIGNAL")
        if numbers:
            reason_codes.append("NUMERIC_SIGNAL")
        if structural:
            reason_codes.append("TABLE_SIGNAL")
        if negations:
            reason_codes.append("NEGATION_PRESERVED")
        conversation = tuple(
            {
                "sha256": hashlib.sha256(turn.encode("utf-8")).hexdigest(),
                "length": len(turn),
            }
            for turn in request.conversation_context[-8:]
        )
        return QueryAnalysis(
            original_query=request.text,
            normalized_query=normalized,
            quoted_phrases=quoted,
            identifiers=identifiers,
            numbers=numbers,
            units=units,
            date_version_signals=dates,
            language_hints=tuple(language),
            structural_table_signals=structural,
            negation_signals=negations,
            conversation_fingerprint=canonical_sha256(conversation),
            reason_codes=tuple(reason_codes),
        )


def _looks_like_phone(value: str) -> bool:
    compact = re.sub(r"\D", "", value)
    return (
        not re.search(r"[A-Za-z]", value) and len(compact) >= _PHONE_DIGIT_COUNT
    )


__all__ = ["QueryAnalyzer"]
