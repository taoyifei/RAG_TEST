"""在真实只读 DOCX 上比较 production candidate 与冻结基线。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(
        0,
        str(Path(__file__).resolve().parents[1] / "src"),
    )

from evaluation.legacy_chunking import (
    fixed_token_windows,
    legacy_element_chunks,
)
from rag_app.chunking import (
    Chunker,
    ChunkerConfig,
    HuggingFaceTokenCounter,
)
from rag_app.contracts import (
    PipelineSpec,
    allocate_source_id,
    content_doc_version,
)
from rag_app.corpus_policy import CorpusPolicy
from rag_app.parsers.docx import DocxParser
from rag_app.runtime import load_pipeline

_FIXED_BASELINE_TOKENS = 512


def summarize_token_lengths(lengths: Sequence[int]) -> dict[str, int]:
    """汇总 token 长度的确定性分位数。

    Args:
        lengths: 非空的 token 长度序列。

    Returns:
        数量、最小值、p50、p90、p95 和最大值。

    Raises:
        ValueError: 输入为空或包含负数。

    """
    if not lengths:
        raise ValueError("lengths 不能为空。")
    if any(length < 0 for length in lengths):
        raise ValueError("token 长度不能为负。")
    ordered = sorted(lengths)
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p50": _nearest_rank(ordered, 0.50),
        "p90": _nearest_rank(ordered, 0.90),
        "p95": _nearest_rank(ordered, 0.95),
        "maximum": ordered[-1],
    }


def run_experiment(
    input_directory: Path,
    tokenizer_path: Path,
    pipeline_path: Path,
    corpus_policy_path: Path,
) -> dict[str, object]:
    """用 operator 指定配置在真实 DOCX 上运行结构实验。

    Args:
        input_directory: 只读 DOCX 输入目录。
        tokenizer_path: 已固化的 embedding tokenizer.json。
        pipeline_path: operator 指定的严格 pipeline.json。
        corpus_policy_path: operator 指定的 corpus-policy.json。

    Returns:
        不含文件名、标题、正文或 quote 的 JSON 兼容聚合摘要。

    Raises:
        ValueError: 配置、资产摘要、parser revision 或 corpus policy 不一致。

    """
    pipeline = load_pipeline(pipeline_path)
    policy = CorpusPolicy.load(corpus_policy_path)
    _validate_inputs(
        pipeline,
        policy,
        tokenizer_path=tokenizer_path,
    )
    token_counter = HuggingFaceTokenCounter(tokenizer_path)
    config = _chunker_config(pipeline)
    chunker = Chunker(
        config,
        token_counter,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    parser = DocxParser()
    paths = _docx_paths(input_directory)
    relative_paths = tuple(
        path.relative_to(input_directory).as_posix() for path in paths
    )
    metadata = policy.resolve(
        input_root=input_directory,
        discovered_paths=relative_paths,
    )
    structural_text_lengths: list[int] = []
    structural_embedding_lengths: list[int] = []
    structural_kinds: Counter[str] = Counter()
    legacy_text_lengths: list[int] = []
    legacy_embedding_lengths: list[int] = []
    legacy_kinds: Counter[str] = Counter()
    fixed_lengths: list[int] = []
    for path, relative_path in zip(paths, relative_paths, strict=True):
        content_sha256 = _sha256_file(path)
        elements = parser.parse(path, display_path=relative_path)
        chunks = chunker.chunk(
            source_id=allocate_source_id(relative_path, content_sha256),
            doc_version=content_doc_version(content_sha256),
            elements=elements,
            metadata=metadata[relative_path],
        )
        structural_text_lengths.extend(
            token_counter.count(chunk.text) for chunk in chunks
        )
        structural_embedding_lengths.extend(
            token_counter.count(chunk.embedding_text) for chunk in chunks
        )
        structural_kinds.update(
            chunk.element_kind.value for chunk in chunks
        )
        legacy = legacy_element_chunks(elements, token_counter, config)
        legacy_text_lengths.extend(
            token_counter.count(chunk.text) for chunk in legacy
        )
        legacy_embedding_lengths.extend(
            token_counter.count(chunk.embedding_text) for chunk in legacy
        )
        legacy_kinds.update(chunk.element_kind.value for chunk in legacy)
        fixed_lengths.extend(
            token_counter.count(window.text)
            for window in fixed_token_windows(
                elements,
                token_counter,
                window_tokens=_FIXED_BASELINE_TOKENS,
            )
        )
    return {
        "documents": len(paths),
        "pipeline_fingerprint": pipeline.fingerprint(),
        "tokenizer_sha256": _sha256_file(tokenizer_path),
        "structural": _chunk_summary(
            config,
            structural_text_lengths,
            structural_embedding_lengths,
            structural_kinds,
        ),
        "legacy_element": _chunk_summary(
            config,
            legacy_text_lengths,
            legacy_embedding_lengths,
            legacy_kinds,
        ),
        "fixed_512": {
            "window_tokens": _FIXED_BASELINE_TOKENS,
            "text_token_lengths": summarize_token_lengths(fixed_lengths),
            "embedding_token_lengths": summarize_token_lengths(
                fixed_lengths
            ),
        },
        "status": "structural_only_provisional",
    }


def _chunk_summary(
    config: ChunkerConfig,
    text_lengths: list[int],
    embedding_lengths: list[int],
    kinds: Counter[str],
) -> dict[str, object]:
    return {
        "config": {
            "target_tokens": config.target_tokens,
            "hard_max_tokens": config.hard_max_tokens,
            "overlap_tokens": config.overlap_tokens,
        },
        "text_token_lengths": summarize_token_lengths(text_lengths),
        "embedding_token_lengths": summarize_token_lengths(
            embedding_lengths
        ),
        "chunks_by_kind": dict(sorted(kinds.items())),
    }


def _validate_inputs(
    pipeline: PipelineSpec,
    policy: CorpusPolicy,
    *,
    tokenizer_path: Path,
) -> None:
    tokenizer_sha256 = _sha256_file(tokenizer_path)
    if tokenizer_sha256 != pipeline.embedding_tokenizer_sha256:
        raise ValueError("embedding tokenizer SHA256 与 pipeline 不一致。")
    if policy.semantic_sha256() != pipeline.corpus_policy_sha256:
        raise ValueError("corpus policy SHA256 与 pipeline 不一致。")
    if pipeline.parser_revision != DocxParser.version:
        raise ValueError("parser revision 与 DocxParser 不一致。")


def _chunker_config(pipeline: PipelineSpec) -> ChunkerConfig:
    parameters = dict(pipeline.chunker_parameters)
    try:
        return ChunkerConfig(
            target_tokens=int(parameters["target_tokens"]),
            hard_max_tokens=int(parameters["hard_max_tokens"]),
            overlap_tokens=int(parameters["overlap_tokens"]),
        )
    except (KeyError, ValueError) as error:
        raise ValueError("pipeline chunker_parameters 无效。") from error


def _docx_paths(input_directory: Path) -> list[Path]:
    return sorted(
        path
        for path in input_directory.rglob("*.docx")
        if "Zone.Identifier" not in path.name
    )


def _nearest_rank(ordered: Sequence[int], quantile: float) -> int:
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    """执行只输出非敏感聚合统计的真实 DOCX 结构实验。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("input_directory", type=Path)
    argument_parser.add_argument("--tokenizer", required=True, type=Path)
    argument_parser.add_argument("--pipeline", required=True, type=Path)
    argument_parser.add_argument(
        "--corpus-policy",
        required=True,
        type=Path,
    )
    argument_parser.add_argument("--output", type=Path)
    arguments = argument_parser.parse_args()
    result = run_experiment(
        arguments.input_directory,
        arguments.tokenizer,
        arguments.pipeline,
        arguments.corpus_policy,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.output is None:
        print(rendered)
    else:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
