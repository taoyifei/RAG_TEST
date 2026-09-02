"""结构化分块 CLI 的离线 composition helpers。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rag_app.adapters.chunkers import DocxStructuralChunker
from rag_app.adapters.tokenizers import DeterministicUtf8TokenCounter
from rag_app.composition.factory import build_components
from rag_app.composition.profiles import default_offline_profile, load_profile
from rag_app.composition.registry import (
    ComponentRegistry,
    register_builtin_components,
)
from rag_app.core.errors import RagError
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    ChunkingPolicy,
    DocumentIR,
    DocumentRef,
    ParseContext,
    ParseSource,
)
from rag_app.core.policies import (
    CommentsPolicy,
    ParsingPolicy,
    StoryPolicy,
)
from rag_app.core.ports import ParserPort

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_ABLATION_CANDIDATES = (
    (256, 512, 32),
    (320, 512, 48),
    (384, 512, 64),
)


def chunk_document_command(
    path: Path,
    *,
    profile_path: Path | None,
    include_content: bool,
) -> int:
    """解析并分块一个 DOCX，默认只输出统计和 ID 前缀。

    Args:
        path: 用户显式指定的本地 DOCX。
        profile_path: 可选严格 Profile。
        include_content: 是否显式输出三种文本视图。

    Returns:
        成功返回 0。

    Raises:
        FileNotFoundError: 输入不是现有非 symlink 普通文件。
        RuntimeError: Profile 未选择结构化 V3 Chunker。

    """
    _require_document(path)
    profile = (
        load_profile(profile_path)
        if profile_path
        else default_offline_profile()
    )
    registry = ComponentRegistry()
    register_builtin_components(registry)
    with build_components(profile, registry) as components:
        document_ir = _parse_document(path, components.parser)
        chunker = components.chunker
        fingerprint = getattr(chunker, "fingerprint", None)
        if not isinstance(fingerprint, str):
            raise RuntimeError("chunk-document 必须选择 docx-structural-v3。")
        result = chunker.chunk(
            document_ir,
            ChunkingContext(
                chunker_fingerprint=fingerprint,
                index_revision_id=deterministic_id(
                    "irev",
                    document_ir.version.document_version_id,
                    fingerprint,
                ),
            ),
        )
    report = result.report
    print(
        f"chunker=docx-structural-v3 chunks={report.chunk_count} "
        f"token_p95={report.token_p95} token_max={report.token_max}"
    )
    print(
        f"roles={dict(report.chunk_count_by_role)} "
        f"source_span_coverage={report.source_span_coverage:.6f}"
    )
    print(
        "chunk_id_prefixes="
        + ",".join(chunk.chunk_id[:18] for chunk in result.chunks)
    )
    if include_content:
        payload = [
            chunk.model_dump(mode="json", exclude_none=False)
            for chunk in result.chunks
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def chunk_ablation_command(
    path: Path,
    *,
    output_directory: Path,
    profile_path: Path | None,
) -> int:
    """对同一 IR snapshot 比较三个 provisional 参数候选。

    Args:
        path: 单个 DOCX 或只读取直接子 DOCX 的目录。
        output_directory: JSON 和 Markdown 输出目录。
        profile_path: 可选 Parser Profile。

    Returns:
        全部文档和候选成功时返回 0。

    Raises:
        FileNotFoundError: 输入路径无效或目录没有 DOCX。

    """
    documents = _document_paths(path)
    profile = (
        load_profile(profile_path)
        if profile_path
        else default_offline_profile()
    )
    registry = ComponentRegistry()
    register_builtin_components(registry)
    snapshots: list[tuple[Path, DocumentIR]] = []
    rejected: list[dict[str, object]] = []
    with build_components(profile, registry) as components:
        for document in documents:
            try:
                snapshots.append(
                    (
                        document,
                        _parse_document(document, components.parser),
                    )
                )
            except RagError as error:
                rejected.append(
                    {
                        "document": document.name,
                        "status": "parser_rejected",
                        "error_code": error.code,
                        "error_stage": error.stage,
                    }
                )
    rows: list[dict[str, object]] = [*rejected]
    for target, hard_max, overlap in _ABLATION_CANDIDATES:
        policy = ChunkingPolicy(
            target_tokens=target,
            hard_max_tokens=hard_max,
            overlap_cap_tokens=overlap,
        )
        chunker = DocxStructuralChunker(
            policy,
            DeterministicUtf8TokenCounter(),
        )
        for document, snapshot in snapshots:
            result = chunker.chunk(
                snapshot,
                ChunkingContext(
                    chunker_fingerprint=chunker.fingerprint,
                    index_revision_id=deterministic_id(
                        "irev",
                        snapshot.version.document_version_id,
                        chunker.fingerprint,
                    ),
                ),
            )
            rows.append(
                {
                    "document": document.name,
                    "status": "chunked",
                    "target_tokens": target,
                    "hard_max_tokens": hard_max,
                    "overlap_cap_tokens": overlap,
                    "provisional": True,
                    "report": result.report.model_dump(
                        mode="json",
                        exclude_none=False,
                    ),
                }
            )
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "chunk-ablation.json"
    markdown_path = output_directory / "chunk-ablation.md"
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_ablation_markdown(rows), encoding="utf-8")
    print(
        f"documents={len(documents)} candidates={len(_ABLATION_CANDIDATES)} "
        f"chunk_runs={len(rows) - len(rejected)} rejected={len(rejected)}"
    )
    print(f"output_json={json_path}")
    print(f"output_markdown={markdown_path}")
    print("selection=provisional; freeze_in=P08")
    return 0


def _parse_document(path: Path, parser: ParserPort) -> DocumentIR:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    document = DocumentRef(
        project_id=deterministic_id("prj", "chunk-cli"),
        knowledge_base_id=deterministic_id("kb", "chunk-cli"),
        document_id=deterministic_id("doc", digest),
        display_name=path.name,
    )
    policy = ParsingPolicy(
        comments=CommentsPolicy.INCLUDE,
        headers_footers=StoryPolicy.PARSE,
        footnotes_endnotes=StoryPolicy.PARSE,
    )
    result = parser.parse(
        ParseSource(
            media_type=_DOCX_MEDIA_TYPE,
            display_name=path.name,
            extension=path.suffix,
            content=content,
        ),
        policy,
        ParseContext(document=document),
    )
    return result.document_ir


def _require_document(path: Path) -> None:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.suffix.lower() != ".docx"
    ):
        raise FileNotFoundError("分块输入必须是现有非 symlink DOCX 文件。")


def _document_paths(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        _require_document(path)
        return (path,)
    if path.is_symlink() or not path.is_dir():
        raise FileNotFoundError("消融输入必须是 DOCX 或现有非 symlink 目录。")
    documents = tuple(
        item
        for item in sorted(path.iterdir())
        if item.is_file()
        and not item.is_symlink()
        and item.suffix.lower() == ".docx"
    )
    if not documents:
        raise FileNotFoundError("消融目录没有直接子 DOCX。")
    return documents


def _ablation_markdown(rows: list[dict[str, object]]) -> str:
    lines = [
        "# Chunking V3 provisional structural ablation",
        "",
        "本报告不调用真实 Embedding，也不选择最佳参数；P08 才冻结。",
        "",
        "| 文档 | target/hard/overlap | chunks | p95 | max | warnings |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        report = row.get("report")
        if row.get("status") == "parser_rejected":
            lines.append(
                f"| {row['document']} | parser rejected | 0 | 0 | 0 | "
                f"{row['error_code']} |"
            )
            continue
        if not isinstance(report, dict):
            raise TypeError("ablation report 必须是 JSON object。")
        lines.append(
            f"| {row['document']} | {row['target_tokens']}/"
            f"{row['hard_max_tokens']}/{row['overlap_cap_tokens']} | "
            f"{report['chunk_count']} | {report['token_p95']} | "
            f"{report['token_max']} | {len(report['warnings'])} |"
        )
    return "\n".join(lines) + "\n"
