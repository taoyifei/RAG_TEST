"""候选自然问答的统一模型预算与消息估算。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.application.answering.natural_answer import NaturalMessage
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.tokenization import estimate_tokens

NATURAL_PIPELINE_REVISION = "weknora-natural-v3-02"
_MESSAGE_OVERHEAD = 16  # 与现有 Chat adapter 的消息封装估算一致。
_MIN_NATURAL_INPUT_TOKENS = 256


@dataclass(frozen=True, slots=True)
class NaturalBudget:
    """只作用于候选自然链的模型与阅读材料上限。"""

    context_window: int = 8192
    input_cap: int = 5000
    # 在既有 8192 上下文、5000 输入上限和 512 预留内用足可用输出空间。
    output_tokens: int = 2680
    safety_margin: int = 512
    max_passages: int = 8
    rerank_pool: int = 24
    rewrite_output_tokens: int = 160

    def __post_init__(self) -> None:
        """拒绝会导致消息或来源材料无法装包的配置。"""
        if (
            min(
                self.context_window,
                self.input_cap,
                self.output_tokens,
                self.safety_margin,
                self.max_passages,
                self.rerank_pool,
                self.rewrite_output_tokens,
            )
            <= 0
        ):
            raise ValueError("自然问答预算必须为正数。")
        if self.input_limit < _MIN_NATURAL_INPUT_TOKENS:
            raise ValueError("自然问答输入预算不足。")

    @property
    def input_limit(self) -> int:
        """为输出及未知模板开销留余量后的输入上限。"""
        return min(
            self.input_cap,
            self.context_window - self.output_tokens - self.safety_margin,
        )

    @property
    def identity(self) -> str:
        """供候选 trace 与结果区分新旧语义。"""
        return canonical_sha256(
            {"pipeline": NATURAL_PIPELINE_REVISION, **self._identity_values()}
        )

    def _identity_values(self) -> dict[str, int]:
        return {
            "context_window": self.context_window,
            "input_cap": self.input_cap,
            "output_tokens": self.output_tokens,
            "safety_margin": self.safety_margin,
            "max_passages": self.max_passages,
            "rerank_pool": self.rerank_pool,
            "rewrite_output_tokens": self.rewrite_output_tokens,
        }


def estimate_natural_messages(messages: tuple[NaturalMessage, ...]) -> int:
    """与发送 adapter 共用字符估算口径，并标记为估算值。"""
    return sum(
        estimate_tokens(item.content) + _MESSAGE_OVERHEAD for item in messages
    )


__all__ = [
    "NATURAL_PIPELINE_REVISION",
    "NaturalBudget",
    "estimate_natural_messages",
]
