"""从默认 Product 组合根生成脱敏的 Word 分层基线。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    Chunk,
    ChunkingContext,
    ChunkingReport,
    DocumentIR,
    DocumentRef,
    ParseContext,
    ParsedArtifact,
    ParseSource,
    validate_document_ir,
)
from rag_app.product.provider_runtime import build_offline_mock_transport

_DOC_MEDIA_TYPE = "application/msword"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def _detect_format(content: bytes) -> tuple[str, str]:
    """根据实际文件签名返回扩展名与媒体类型。

    Args:
        content: Word 文件原始字节。

    Returns:
        实际扩展名和媒体类型。

    Raises:
        ValueError: 文件签名不是受支持的 DOC 或 DOCX。

    """
    if content.startswith(_OLE_MAGIC):
        return ".doc", _DOC_MEDIA_TYPE
    if content.startswith(_ZIP_MAGIC):
        return ".docx", _DOCX_MEDIA_TYPE
    raise ValueError("文件签名不是受支持的 OLE DOC 或 OOXML DOCX。")


def _runtime_settings(root: Path) -> ProductRuntimeSettings:
    """创建无网络、无真实凭据的临时 Product 设置。"""
    repository_root = Path(__file__).resolve().parents[1]
    frontend = root / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>V3-00 Word probe</title>",
        encoding="utf-8",
    )
    bootstrap = root / "bootstrap-token"
    bootstrap.write_text(
        "v3-00-synthetic-bootstrap-token",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    return ProductRuntimeSettings(
        data_dir=root / "data",
        frontend_dir=frontend,
        bootstrap_token_file=bootstrap,
        qdrant_mode="memory",
        compatibility_manifest=repository_root / "compatibility-manifest.json",
        migrations_dir=repository_root / "migrations" / "universal_rag",
    )


def _normalize_ir(document: DocumentIR) -> dict[str, object]:
    """移除解析耗时，保留身份、结构和来源语义。"""
    payload = cast(dict[str, object], document.model_dump(mode="json"))
    report = cast(dict[str, object], payload["parse_report"])
    report["elapsed_seconds"] = 0.0
    return payload


def _normalize_chunks(
    chunks: Sequence[Chunk], report: ChunkingReport
) -> dict[str, object]:
    """移除分块耗时，保留三视图、边界和 SourceSpan。"""
    report_payload = cast(dict[str, object], report.model_dump(mode="json"))
    report_payload["elapsed_seconds"] = 0.0
    return {
        "chunks": [item.model_dump(mode="json") for item in chunks],
        "report": report_payload,
    }


def _artifact_summary(
    artifacts: Sequence[ParsedArtifact],
) -> list[dict[str, object]]:
    """只导出 Artifact 身份和类型，不复制正文或媒体字节。"""
    return [
        {
            "artifact_id": item.artifact_id,
            "content_sha256": item.content_sha256,
            "media_type": item.media_type,
            "role": item.role,
            "size_bytes": len(item.content),
        }
        for item in artifacts
    ]


def _node_summary(document: DocumentIR) -> dict[str, object]:
    """汇总节点、图片实例、占位符和关系，不推断图中语义。"""
    kinds = Counter(node.kind.value for node in document.nodes)
    image_nodes = [
        node for node in document.nodes if node.image_attributes is not None
    ]
    media_hashes = {
        node.image_attributes.content_sha256
        for node in image_nodes
        if node.image_attributes is not None
    }
    return {
        "node_count": len(document.nodes),
        "node_kinds": dict(sorted(kinds.items())),
        "visible_text_nodes": document.parse_report.visible_text_nodes,
        "represented_visible_text_nodes": (
            document.parse_report.represented_visible_text_nodes
        ),
        "image_display_instances": len(image_nodes),
        "unique_embedded_media": len(media_hashes),
        "pic_placeholder_count": sum(
            (node.text or "").casefold().count("[pic]")
            for node in document.nodes
        ),
        "relationship_count": len(document.relationships),
        "relationship_types": sorted(
            {item.relationship_type for item in document.relationships}
        ),
    }


def _chunk_summary(chunks: Sequence[Chunk]) -> dict[str, object]:
    """汇总可检索三视图、来源跨度与邻居关系。"""
    roles = Counter(item.role.value for item in chunks)
    spans = Counter(
        span.span_type.value for item in chunks for span in item.source_spans
    )
    return {
        "chunk_count": len(chunks),
        "roles": dict(sorted(roles.items())),
        "source_span_kinds": dict(sorted(spans.items())),
        "citation_view_sha256": [
            hashlib.sha256(item.citation_text.encode()).hexdigest()
            for item in chunks
        ],
        "embedding_view_sha256": [
            hashlib.sha256(item.embedding_text.encode()).hexdigest()
            for item in chunks
        ],
        "lexical_view_sha256": [
            hashlib.sha256(item.lexical_text.encode()).hexdigest()
            for item in chunks
        ],
        "neighbor_links": sum(
            item.previous_chunk_id is not None or item.next_chunk_id is not None
            for item in chunks
        ),
    }


def classify_media_inventory(document: DocumentIR) -> tuple[str, str]:
    """区分完整、部分和无法证明的媒体盘点。

    Args:
        document: Parser 或增补器输出的规范 IR。

    Returns:
        媒体盘点状态及零计数的安全解释。

    """
    flattened = any(
        item.code == "LEGACY_DOC_FLATTENED_TEXT"
        for item in document.parse_report.issues
    )
    has_image_instance = any(
        node.image_attributes is not None for node in document.nodes
    )
    if flattened and not has_image_instance:
        return "unknown", "unknown_after_flattening"
    declared = dict(document.metadata).get("media_inventory")
    if declared == "unknown":
        return "unknown", "unknown_source_inventory"
    if declared == "partial" or document.parse_report.unsupported_with_media:
        return "partial", "partial_with_unresolved_media"
    return "complete", "complete_by_package_inventory"


def run_probe(
    input_path: Path,
    *,
    include_content: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """运行默认 Product 的 parse、OCR scan/enrich 与 chunk 链。

    Args:
        input_path: 用户明确指定的本地 Word 文件。
        include_content: 是否在结果中加入规范化 IR 与三视图正文。
        expected_sha256: 可选的预期输入 SHA-256。

    Returns:
        不含凭据、绝对路径或原始二进制的分层证据。

    Raises:
        ValueError: 扩展名、签名或预期摘要不一致。

    """
    if input_path.is_symlink():
        raise ValueError("输入必须是可读取的普通文件且不能是 symlink。")
    path = input_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("输入必须是可读取的普通文件且不能是 symlink。")
    content = path.read_bytes()
    content_sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and content_sha256 != expected_sha256:
        raise ValueError("输入 SHA-256 与预期值不一致。")
    detected_extension, media_type = _detect_format(content)
    declared_extension = path.suffix.casefold()
    if declared_extension != detected_extension:
        raise ValueError("Word 扩展名与文件签名不一致。")

    with (
        tempfile.TemporaryDirectory(prefix="rag-v3-00-word-") as temporary,
        build_product_runtime(
            _runtime_settings(Path(temporary)),
            transport_factory=build_offline_mock_transport,
            recover_jobs=False,
        ) as runtime,
    ):
        project = runtime.sdk.create_project(
            "V3-00 合成探针",
            idempotency_key="v3-00-word-probe-project",
        )
        knowledge_base = runtime.sdk.create_knowledge_base(
            project.project_id,
            "V3-00 合成知识库",
            idempotency_key="v3-00-word-probe-kb",
        )
        document = DocumentRef(
            project_id=project.project_id,
            knowledge_base_id=knowledge_base.knowledge_base_id,
            document_id=deterministic_id(
                "doc",
                project.project_id,
                knowledge_base.knowledge_base_id,
                "v3-00-word-probe-document",
            ),
            display_name=path.name,
        )
        components = runtime.p09.retrieval_runtime.persistence.components
        parsed = components.parser.parse(
            ParseSource(
                media_type=media_type,
                display_name=path.name,
                content=content,
                extension=detected_extension,
            ),
            components.parsing_policy,
            ParseContext(document=document),
        )
        validate_document_ir(parsed.document_ir)
        media_scan = runtime.ocr.scan_document(
            parsed.document_ir,
            artifacts=parsed.artifacts,
        )
        enriched = runtime.ocr.enrich_result(parsed)
        validate_document_ir(enriched.document_ir)
        fingerprint = getattr(components.chunker, "fingerprint", None)
        if not isinstance(fingerprint, str):
            raise TypeError("默认 Product Chunker 缺少冻结 fingerprint。")
        chunked = components.chunker.chunk(
            enriched.document_ir,
            ChunkingContext(
                chunker_fingerprint=fingerprint,
                index_revision_id=deterministic_id(
                    "irev",
                    enriched.document_ir.version.document_version_id,
                    fingerprint,
                ),
            ),
        )

    issue_codes = [item.code for item in enriched.report.issues]
    flattened_doc = "LEGACY_DOC_FLATTENED_TEXT" in issue_codes
    node_summary = _node_summary(enriched.document_ir)
    media_inventory_status, zero_count_meaning = classify_media_inventory(
        enriched.document_ir
    )
    document_metadata = dict(enriched.document_ir.metadata)
    normalized_ir = _normalize_ir(enriched.document_ir)
    normalized_chunks = _normalize_chunks(chunked.chunks, chunked.report)
    result: dict[str, Any] = {
        "schema_version": "v3-00-word-baseline-1",
        "status": (
            "PARTIAL"
            if flattened_doc or media_inventory_status != "complete"
            else "PASS"
        ),
        "input": {
            "display_name": path.name,
            "size_bytes": len(content),
            "content_sha256": content_sha256,
            "declared_extension": declared_extension,
            "detected_extension": detected_extension,
            "detected_media_type": media_type,
            "magic_prefix_hex": content[:8].hex(),
        },
        "product_components": {
            "parser": components.parser.descriptor.model_dump(mode="json"),
            "chunker": components.chunker.descriptor.model_dump(mode="json"),
            "parsing_policy": components.parsing_policy.model_dump(mode="json"),
            "chunking_policy": components.chunking_policy.model_dump(
                mode="json"
            ),
        },
        "parse": {
            **node_summary,
            "native_text": document_metadata.get("native_text", "native"),
            "structure_coverage": document_metadata.get(
                "source_representation", "native-docx"
            ),
            "relationship_status": document_metadata.get(
                "relationship_status", "native-package"
            ),
            "issue_codes": issue_codes,
            "warnings": list(enriched.report.warnings),
            "artifacts": _artifact_summary(enriched.artifacts),
            "normalized_ir_sha256": canonical_sha256(normalized_ir),
        },
        "media_scan": {
            **media_scan,
            "inventory_status": media_inventory_status,
            "zero_count_meaning": zero_count_meaning,
        },
        "chunking": {
            **_chunk_summary(chunked.chunks),
            "normalized_chunks_sha256": canonical_sha256(normalized_chunks),
            "source_span_coverage": chunked.report.source_span_coverage,
            "missing_source_chars": chunked.report.missing_source_chars,
        },
    }
    if include_content:
        result["private_normalized_snapshot"] = {
            "document_ir": normalized_ir,
            **normalized_chunks,
        }
    return result


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行默认 Product Word 分层基线，不调用真实 Provider。"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--include-content", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """运行命令行探针并输出 UTF-8 JSON。

    Args:
        argv: 可选命令行参数；缺失时读取当前进程参数。

    Returns:
        成功时返回 0。

    """
    args = _arguments(argv)
    payload = run_probe(
        args.input,
        include_content=args.include_content,
        expected_sha256=args.expected_sha256,
    )
    output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is None:
        print(output)
    else:
        target = args.output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
