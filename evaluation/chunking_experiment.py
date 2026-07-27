"""比较结构切块与固定 512-token 基线。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from rag_app.chunking import (
    Chunker,
    ChunkerConfig,
    HuggingFaceTokenCounter,
)
from rag_app.contracts import (
    Element,
    ElementKind,
    OcrState,
    PipelineSpec,
    allocate_source_id,
    content_doc_version,
)
from rag_app.parsers.docx import DocxParser

_STRUCTURAL_CONFIG = ChunkerConfig(
    target_tokens=384,
    hard_max_tokens=512,
    overlap_tokens=64,
)
_FIXED_BASELINE_TOKENS = 512


def summarize_token_lengths(lengths: Sequence[int]) -> dict[str, int]:
    """汇总 token 长度的确定性分位数。

    Args:
        lengths: 非空的 token 长度序列。

    Returns:
        数量、最小值、p50、p95 和最大值。

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
        "p95": _nearest_rank(ordered, 0.95),
        "maximum": ordered[-1],
    }


def run_experiment(
    input_directory: Path,
    tokenizer_path: Path,
) -> dict[str, object]:
    """在冻结 DOCX 上比较两种切块方式的静态分布。

    Args:
        input_directory: 只读 DOCX 输入目录。
        tokenizer_path: Qwen 本地 tokenizer.json。

    Returns:
        JSON 兼容的实验摘要，不包含检索效果结论。

    """
    token_counter = HuggingFaceTokenCounter(tokenizer_path)
    pipeline = _experiment_pipeline()
    chunker = Chunker(
        _STRUCTURAL_CONFIG,
        token_counter,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    parser = DocxParser()
    structural_lengths: list[int] = []
    fixed_lengths: list[int] = []
    structural_kinds: Counter[str] = Counter()
    documents = 0
    for path in _docx_paths(input_directory):
        documents += 1
        relative_path = path.relative_to(input_directory).as_posix()
        content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        elements = parser.parse(path, display_path=relative_path)
        chunks = chunker.chunk(
            source_id=allocate_source_id(relative_path, content_sha256),
            doc_version=content_doc_version(content_sha256),
            elements=elements,
        )
        structural_lengths.extend(
            token_counter.count(chunk.text) for chunk in chunks
        )
        structural_kinds.update(chunk.element_kind.value for chunk in chunks)
        fixed_lengths.extend(
            _fixed_window_lengths(elements, token_counter)
        )
    return {
        "documents": documents,
        "tokenizer_sha256": hashlib.sha256(
            tokenizer_path.read_bytes()
        ).hexdigest(),
        "structural": {
            "config": {
                "target_tokens": _STRUCTURAL_CONFIG.target_tokens,
                "hard_max_tokens": _STRUCTURAL_CONFIG.hard_max_tokens,
                "overlap_tokens": _STRUCTURAL_CONFIG.overlap_tokens,
            },
            "token_lengths": summarize_token_lengths(structural_lengths),
            "chunks_by_kind": dict(sorted(structural_kinds.items())),
        },
        "fixed_512": {
            "window_tokens": _FIXED_BASELINE_TOKENS,
            "token_lengths": summarize_token_lengths(fixed_lengths),
        },
        "conclusion": (
            "仅完成静态分布对照；必须在冻结问答集上完成召回消融后才能定参。"
        ),
    }


def _docx_paths(input_directory: Path) -> list[Path]:
    return sorted(
        path
        for path in input_directory.rglob("*.docx")
        if "Zone.Identifier" not in path.name
    )


def _fixed_window_lengths(
    elements: Sequence[Element],
    token_counter: HuggingFaceTokenCounter,
) -> list[int]:
    evidence_texts = [
        element.text
        for element in elements
        if element.text
        and (
            element.kind != ElementKind.IMAGE
            or element.ocr_state
            in {OcrState.SUCCEEDED, OcrState.LOW_CONFIDENCE}
        )
    ]
    total_tokens = token_counter.count("\n".join(evidence_texts))
    if total_tokens == 0:
        return []
    full_windows, remainder = divmod(total_tokens, _FIXED_BASELINE_TOKENS)
    lengths = [_FIXED_BASELINE_TOKENS] * full_windows
    if remainder:
        lengths.append(remainder)
    return lengths


def _nearest_rank(ordered: Sequence[int], quantile: float) -> int:
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _experiment_pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="1",
        parser_revision=DocxParser.version,
        ocr_model="server-gpu-ocr-unselected",
        ocr_revision="unselected",
        chunker_revision="structural-v1",
        chunker_parameters=(
            ("target_tokens", "384"),
            ("hard_max_tokens", "512"),
            ("overlap_tokens", "64"),
        ),
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_revision="pending-server-validation",
        embedding_dimension=1024,
        sparse_model="bm25-chinese",
        sparse_revision="pending-benchmark",
        index_revision="qdrant-v1.18.3",
        reranker_model="Qwen3-Reranker-0.6B",
        reranker_revision="pending-server-validation",
        llm_revisions=(("Qwen3-8B-AWQ", "pending-remote-revision"),),
        prompt_revision="strict-answer-v1",
    )


def main() -> None:
    """执行结构切块静态实验。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("input_directory", type=Path)
    argument_parser.add_argument("tokenizer_path", type=Path)
    argument_parser.add_argument("--output", type=Path)
    arguments = argument_parser.parse_args()
    result = run_experiment(
        arguments.input_directory,
        arguments.tokenizer_path,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.output is None:
        print(rendered)
    else:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
