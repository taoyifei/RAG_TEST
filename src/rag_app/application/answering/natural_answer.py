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
_REFUSAL = ("资料不足", "无法根据", "没有找到", "无法确认", "未找到")


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
    citation_basis: Literal["original", "parsed_artifact", "mixed"]
    source_complete: bool


class NaturalAnswerResult(FrozenModel):
    """候选链独立终态，不伪装为逐 Claim 的旧结果。"""

    trace_id: str
    engine_id: Literal["wk-standard-v1", "wk-standard-pc-v1"]
    answer: str | None = Field(default=None, repr=False)
    reason_code: str
    references: tuple[NaturalReference, ...] = ()
    cited_aliases: tuple[str, ...] = ()
    validation_level: Literal["citation_binding_only"] = "citation_binding_only"
    active_index_revision_id: str
    index_fingerprint: str
    serving_fingerprint: str
    selected_embedding_slot: str | None = None
    rerank_execution_mode: str
    degraded_reason_codes: tuple[str, ...] = ()
    generation_model: str | None = None
    input_packet_sha256: str | None = None
    estimated_input_tokens: int = 0
    provider_calls: tuple[ProviderCall, ...] = Field(default=(), exclude=True)


def cited_aliases(answer: str, available: frozenset[str]) -> tuple[str, ...]:
    """仅允许回答引用本次实际送模的短来源别名。"""
    markers = _CITATION_LIKE.findall(answer)
    if any(_CITATION.fullmatch(marker) is None for marker in markers):
        raise ValueError("模型回答包含无法识别的来源引用。")
    cited = tuple(
        dict.fromkeys(f"S{match}" for match in _CITATION.findall(answer))
    )
    if not cited or any(alias not in available for alias in cited):
        if any(marker in answer for marker in _REFUSAL) and not cited:
            return ()
        raise ValueError("模型回答缺少有效来源引用，或引用了未提供的来源。")
    return cited


__all__ = [
    "NaturalAnswerPort",
    "NaturalAnswerResult",
    "NaturalCompletion",
    "NaturalMessage",
    "NaturalReference",
    "cited_aliases",
]
