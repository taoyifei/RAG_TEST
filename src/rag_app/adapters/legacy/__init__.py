"""旧实现到 Core 的单向适配入口。"""

from rag_app.adapters.legacy.contracts import (
    LegacyDocxParserAdapter,
    LegacySectionChunkerAdapter,
    legacy_chunk_to_core,
    legacy_element_to_core,
    legacy_span_to_core,
)
from rag_app.adapters.legacy.document_ir import (
    CompatibilityIssue,
    CompatibilityReport,
    LegacyElementCompatibilityAdapter,
    document_ir_to_legacy_elements,
)
from rag_app.adapters.legacy.providers import (
    DeclaredRemoteEmbeddingProvider,
    DeclaredRemoteReranker,
    DeterministicEmbeddingProvider,
    ExtractiveGenerator,
    HotStandbyRouter,
    LexicalOverlapReranker,
    SingleSlotRouter,
)
from rag_app.adapters.legacy.stores import (
    InMemoryBlobStore,
    InMemoryLexicalStore,
    InMemoryVectorStore,
    SqliteMetadataStore,
    SqliteTraceSink,
)

__all__ = [
    "CompatibilityIssue",
    "CompatibilityReport",
    "DeclaredRemoteEmbeddingProvider",
    "DeclaredRemoteReranker",
    "DeterministicEmbeddingProvider",
    "ExtractiveGenerator",
    "HotStandbyRouter",
    "InMemoryBlobStore",
    "InMemoryLexicalStore",
    "InMemoryVectorStore",
    "LegacyDocxParserAdapter",
    "LegacyElementCompatibilityAdapter",
    "LegacySectionChunkerAdapter",
    "LexicalOverlapReranker",
    "SingleSlotRouter",
    "SqliteMetadataStore",
    "SqliteTraceSink",
    "document_ir_to_legacy_elements",
    "legacy_chunk_to_core",
    "legacy_element_to_core",
    "legacy_span_to_core",
]
