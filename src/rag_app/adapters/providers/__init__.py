"""真实远程 Provider 与权威离线 Provider adapters。"""

from rag_app.adapters.providers.aliyun_qwen37 import (
    AliyunQwen37EmbeddingAdapter,
    AliyunQwen37EmbeddingConfig,
)
from rag_app.adapters.providers.deterministic import (
    DeterministicEmbeddingAdapter,
    LexicalOverlapRerankerAdapter,
)
from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
)
from rag_app.adapters.providers.jina import (
    JinaEmbeddingConfig,
    JinaRerankerConfig,
    JinaRerankerV35Adapter,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.adapters.providers.legacy import (
    LegacyInternalRerankerAdapter,
    LegacyTeiEmbeddingAdapter,
)
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleEmbeddingConfig,
    OpenAICompatibleRerankerAdapter,
    OpenAICompatibleRerankerConfig,
    openai_compatible_chat_payload,
)

__all__ = [
    "AliyunQwen37EmbeddingAdapter",
    "AliyunQwen37EmbeddingConfig",
    "DeterministicEmbeddingAdapter",
    "JinaEmbeddingConfig",
    "JinaRerankerConfig",
    "JinaRerankerV35Adapter",
    "JinaV5TextEmbeddingAdapter",
    "LegacyInternalRerankerAdapter",
    "LegacyTeiEmbeddingAdapter",
    "LexicalOverlapRerankerAdapter",
    "OpenAICompatibleChatAdapter",
    "OpenAICompatibleChatConfig",
    "OpenAICompatibleEmbeddingAdapter",
    "OpenAICompatibleEmbeddingConfig",
    "OpenAICompatibleRerankerAdapter",
    "OpenAICompatibleRerankerConfig",
    "ProviderHttpClient",
    "ProviderHttpError",
    "openai_compatible_chat_payload",
]
