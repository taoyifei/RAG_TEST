"""确定性、中英混合且保留关键字面信号的 QueryAnalyzer。"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from rag_app.application.retrieval.semantics import parse_query_semantics
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ConstraintKind,
    QueryAnalysis,
    QueryConstraint,
    QuerySemantics,
    SearchRequest,
)

_QUOTED = re.compile(r'["“](.+?)["”]')
_IDENTIFIER = re.compile(
    r"(?<!\w)(?=[A-Za-z0-9\u3400-\u9fff_.\-/]{3,80}(?!\w))"
    r"(?=[A-Za-z0-9\u3400-\u9fff_.\-/]*[A-Za-z])"
    r"(?=[A-Za-z0-9\u3400-\u9fff_.\-/]*\d)"
    r"[A-Za-z0-9\u3400-\u9fff]+"
    r"(?:[_.\-/][A-Za-z0-9\u3400-\u9fff]+)+(?!\w)"
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
    "数值",
    "单位",
    "合计",
    "table",
    "column",
    "row",
)
_NEGATIONS = (
    "严禁",
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
_QUALIFIER = re.compile(
    r"仅限|仅仅|只限|只有|至少|至多|最多|最少|不超过|不低于|"
    r"不少于|不高于|超过|低于|高于|大于|小于|之前|之后|以前|"
    r"以后|以上|以下|以内|以外|除外|期间|当前|目前|历史|全部|"
    r"所有|任何|任意|每个|每一|仅|只|除|必须|应当|"
    r"\b(?:only|all|any|every|before|after)\b",
    flags=re.IGNORECASE,
)
_COUNT_NUMBER = re.compile(
    r"(?:第)?(?P<number>[零一二三四五六七八九十百两]+)(?=种|类|项|步|条)"
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
        constraints = _query_constraints(normalized)
        semantics = parse_query_semantics(normalized).model_copy(
            update={"constraints": constraints}
        )
        return QueryAnalysis(
            original_query=request.text,
            normalized_query=normalized,
            resolved_query=normalized,
            semantics=semantics,
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

    def resolve(
        self,
        original: QueryAnalysis,
        request: SearchRequest,
        rewritten_text: str,
    ) -> QueryAnalysis:
        """把已通过 guard 的唯一改写合并回原分析。

        Args:
            original: 原始问题的权威分析与硬约束。
            request: 原始 scope、过滤和会话上下文。
            rewritten_text: 已由改写端口接受的唯一候选。

        Returns:
            原问题与硬约束不变、下游共同使用改写语义的分析。

        """
        rewritten = self.analyze(
            request.model_copy(update={"text": rewritten_text})
        )
        semantics = rewritten.semantics.model_copy(
            update={
                "constraints": original.semantics.constraints,
                "source_qualifier": (
                    original.semantics.source_qualifier
                    or rewritten.semantics.source_qualifier
                ),
                "context_qualifier": (
                    original.semantics.context_qualifier
                    or rewritten.semantics.context_qualifier
                ),
                "source": "LLM_REWRITE",
                "reason_codes": (
                    *rewritten.semantics.reason_codes,
                    "QUERY_REWRITE_ACCEPTED",
                ),
            }
        )
        return rewritten.model_copy(
            update={
                "original_query": original.original_query,
                "normalized_query": original.normalized_query,
                "resolved_query": rewritten.normalized_query,
                "semantics": semantics,
                "quoted_phrases": original.quoted_phrases,
                "identifiers": original.identifiers,
                "numbers": original.numbers,
                "units": original.units,
                "date_version_signals": original.date_version_signals,
                "negation_signals": original.negation_signals,
                "conversation_fingerprint": original.conversation_fingerprint,
                "reason_codes": tuple(
                    dict.fromkeys(
                        (*original.reason_codes, "QUERY_REWRITE_ACCEPTED")
                    )
                ),
            }
        )

    def apply_interpretation(
        self,
        original: QueryAnalysis,
        request: SearchRequest,
        standalone_query: str,
        semantics: QuerySemantics,
    ) -> QueryAnalysis:
        """把已校验的模型解释合并回唯一共享 QueryAnalysis。

        Args:
            original: 原问题的权威文本、范围和硬约束。
            request: 原始请求，用于重新提取独立问句的检索信号。
            standalone_query: 已通过表面与硬约束校验的独立问题。
            semantics: 已通过严格字段校验的类型化语义。

        Returns:
            Planner、Evidence、Confidence 与 Generator 共同消费的分析。

        """
        interpreted = self.analyze(
            request.model_copy(update={"text": standalone_query})
        )
        resolved_semantics = semantics.model_copy(
            update={
                "constraints": original.semantics.constraints,
                "context_qualifier": original.semantics.context_qualifier,
                "source": "LLM_INTERPRET",
                "reason_codes": tuple(
                    dict.fromkeys(
                        (*semantics.reason_codes, "QUERY_INTERPRET_ACCEPTED")
                    )
                ),
            }
        )
        return interpreted.model_copy(
            update={
                "original_query": original.original_query,
                "normalized_query": original.normalized_query,
                "resolved_query": interpreted.normalized_query,
                "semantics": resolved_semantics,
                "quoted_phrases": original.quoted_phrases,
                "identifiers": original.identifiers,
                "numbers": original.numbers,
                "units": original.units,
                "date_version_signals": original.date_version_signals,
                "negation_signals": original.negation_signals,
                "conversation_fingerprint": original.conversation_fingerprint,
                "reason_codes": tuple(
                    dict.fromkeys(
                        (*original.reason_codes, "QUERY_INTERPRET_ACCEPTED")
                    )
                ),
            }
        )


def _looks_like_phone(value: str) -> bool:
    compact = re.sub(r"\D", "", value)
    return (
        not re.search(r"[A-Za-z]", value) and len(compact) >= _PHONE_DIGIT_COUNT
    )


def _query_constraints(text: str) -> tuple[QueryConstraint, ...]:
    """按规范化问题位置收集 guard 需要的硬约束。"""
    matches: list[tuple[int, int, ConstraintKind, str]] = []
    patterns = (
        (ConstraintKind.IDENTIFIER, _IDENTIFIER, 0),
        (ConstraintKind.IDENTIFIER, _STANDARD, 0),
        (ConstraintKind.NUMBER, _NUMBER, 0),
        (ConstraintKind.NUMBER, _COUNT_NUMBER, "number"),
        (ConstraintKind.UNIT, _UNITS, 0),
        (ConstraintKind.DATE_VERSION, _DATE_VERSION, 0),
        (ConstraintKind.QUOTED_TEXT, _QUOTED, 1),
        (ConstraintKind.QUALIFIER, _QUALIFIER, 0),
    )
    for kind, pattern, group in patterns:
        for match in pattern.finditer(text):
            start, end = match.span(group)
            raw = match.group(group)
            if kind is ConstraintKind.IDENTIFIER and _looks_like_phone(raw):
                continue
            matches.append((start, end, kind, raw))
    for signal in _NEGATIONS:
        matches.extend(
            ((match.start(), match.end(), ConstraintKind.NEGATION, match[0]))
            for match in re.finditer(re.escape(signal), text, re.IGNORECASE)
        )
    unique = {
        (start, end, kind, raw.casefold()): QueryConstraint(
            kind=kind,
            raw_text=raw,
            normalized_value=raw.casefold(),
            start_char=start,
            end_char=end,
        )
        for start, end, kind, raw in matches
        if raw
    }
    return tuple(
        unique[key]
        for key in sorted(
            unique, key=lambda item: (item[0], item[1], item[2].value)
        )
    )


__all__ = ["QueryAnalyzer"]
