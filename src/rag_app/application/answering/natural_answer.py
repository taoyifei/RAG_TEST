"""候选通用问答链的自然语言合同与引用绑定。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field

from rag_app.core.models import ProviderCall, SourceSpan
from rag_app.core.models.common import FrozenModel
from rag_app.core.ports import CancellationPort

_CITATION = re.compile(r"\[S([0-9]+)\]")
_CITATION_LIKE = re.compile(r"\[S[^\]]*\]")


@dataclass(frozen=True, slots=True)
class NaturalMessage:
    """普通聊天消息，不携带旧 Claim Schema。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class NaturalCompletion:
    """实际聊天调用的完整正文与脱敏计量。"""

    text: str
    model: str
    provider_calls: tuple[ProviderCall, ...]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CitationBinding:
    """独立记录引用语法与别名绑定状态，不推断正文语义。"""

    status: Literal["valid", "missing", "invalid"]
    cited_aliases: tuple[str, ...] = ()
    invalid_markers: tuple[str, ...] = ()


class NaturalAnswerPort(Protocol):
    """由 Product 组合根绑定来源授权的普通聊天端口。"""

    def complete_natural(
        self,
        messages: tuple[NaturalMessage, ...],
        *,
        source_identities: tuple[tuple[str, str], ...],
        cancellation: CancellationPort,
    ) -> NaturalCompletion:
        """使用实际聊天流完成一次生成。"""

    def validate_natural_sources(
        self, source_identities: tuple[tuple[str, str], ...]
    ) -> None:
        """在最终发布前重新核对文档版本与可见性。"""


class NaturalReference(FrozenModel):
    """本次实际送模的单份材料与规范来源。"""

    alias: str
    document_id: str
    document_version_id: str
    document_title: str
    chunk_ids: tuple[str, ...]
    source_spans: tuple[SourceSpan, ...]
    parent_ranges: tuple[tuple[int, int], ...] = ()
    citation_basis: Literal["original", "parsed_artifact", "mixed"]
    source_complete: bool


class NaturalAnswerResult(FrozenModel):
    """候选链独立终态，不伪装为逐 Claim 的旧结果。"""

    trace_id: str
    engine_id: Literal["wk-standard-v1", "wk-standard-pc-v1"]
    answer: str | None = Field(default=None, repr=False)
    draft: str | None = Field(default=None, repr=False)
    reason_code: str
    references: tuple[NaturalReference, ...] = ()
    cited_aliases: tuple[str, ...] = ()
    citation_status: Literal["valid", "missing", "invalid"] = "missing"
    invalid_citations: tuple[str, ...] = ()
    validation_level: Literal["citation_binding_only"] = "citation_binding_only"
    active_index_revision_id: str
    index_fingerprint: str
    serving_fingerprint: str
    pipeline_revision: str = "weknora-natural-v3-02"
    policy_fingerprint: str | None = None
    selected_embedding_slot: str | None = None
    rerank_execution_mode: str
    degraded_reason_codes: tuple[str, ...] = ()
    generation_model: str | None = None
    input_packet_sha256: str | None = None
    estimated_input_tokens: int = 0
    actual_prompt_tokens: int | None = None
    finish_reason: str | None = None
    provider_calls: tuple[ProviderCall, ...] = Field(default=(), exclude=True)


def check_citations(answer: str, available: frozenset[str]) -> CitationBinding:
    """区分缺失与非法引用；只有本次实际送模的别名可绑定。"""
    markers = _CITATION_LIKE.findall(answer)
    invalid = tuple(
        marker for marker in markers if _CITATION.fullmatch(marker) is None
    )
    cited = tuple(
        dict.fromkeys(f"S{match}" for match in _CITATION.findall(answer))
    )
    invalid += tuple(f"[{alias}]" for alias in cited if alias not in available)
    if invalid:
        return CitationBinding("invalid", invalid_markers=invalid)
    if not cited:
        return CitationBinding("missing")
    return CitationBinding("valid", cited_aliases=cited)


def cited_aliases(answer: str, available: frozenset[str]) -> tuple[str, ...]:
    """兼容旧调用方的严格绑定接口。"""
    binding = check_citations(answer, available)
    if binding.status == "invalid":
        raise ValueError("模型回答包含未提供或无法识别的来源引用。")
    return binding.cited_aliases


__all__ = [
    "CitationBinding",
    "NaturalAnswerPort",
    "NaturalAnswerResult",
    "NaturalCompletion",
    "NaturalMessage",
    "NaturalReference",
    "check_citations",
    "cited_aliases",
]
