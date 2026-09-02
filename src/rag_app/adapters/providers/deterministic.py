"""P02 权威离线 Embedding 与 Reranker adapters。"""

from rag_app.adapters.legacy.providers import (
    DeterministicEmbeddingProvider,
    LexicalOverlapReranker,
)


class DeterministicEmbeddingAdapter(DeterministicEmbeddingProvider):
    """保留 P01 输出的确定性非语义 Embedding adapter。"""


class LexicalOverlapRerankerAdapter(LexicalOverlapReranker):
    """保留 P01 行为的确定性词法重排 adapter。"""


__all__ = [
    "DeterministicEmbeddingAdapter",
    "LexicalOverlapRerankerAdapter",
]
