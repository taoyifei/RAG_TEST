import inspect

from rag_app.core.ports import (
    BlobStorePort,
    ChunkerPort,
    EmbeddingPort,
    GeneratorPort,
    LexicalStorePort,
    MetadataStorePort,
    ParserPort,
    RerankerPort,
    TracePort,
    VectorStorePort,
)


def test_all_v1_ports_are_synchronous_protocols() -> None:
    ports = (
        ParserPort,
        ChunkerPort,
        EmbeddingPort,
        RerankerPort,
        VectorStorePort,
        LexicalStorePort,
        MetadataStorePort,
        BlobStorePort,
        GeneratorPort,
        TracePort,
    )
    for port in ports:
        methods = (
            member
            for name, member in inspect.getmembers(port, inspect.isfunction)
            if not name.startswith("_")
        )
        assert all(
            not inspect.iscoroutinefunction(method) for method in methods
        )


def test_store_ports_remain_narrow_by_responsibility() -> None:
    assert hasattr(VectorStorePort, "search")
    assert not hasattr(VectorStorePort, "get")
    assert hasattr(LexicalStorePort, "search")
    assert not hasattr(LexicalStorePort, "validate_revision")
    assert hasattr(MetadataStorePort, "get")
    assert not hasattr(MetadataStorePort, "search")
