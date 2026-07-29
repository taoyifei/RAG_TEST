"""核对冻结 DOCX 输入的解析结果。"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

_PARSER_MODULE = importlib.import_module("rag_app.parsers.docx")
_DOCX_PARSER = _PARSER_MODULE.DocxParser
_UNSAFE_DOCX_ERROR = _PARSER_MODULE.UnsafeDocxError


def audit(input_directory: Path) -> dict[str, object]:
    """解析目录中的全部 DOCX 并汇总元素数量。

    Args:
        input_directory: 冻结输入目录。

    Returns:
        包含逐文档和总计数的 JSON 兼容字典。

    """
    parser = _DOCX_PARSER()
    totals: Counter[str] = Counter()
    unique_media: set[str] = set()
    total_bytes = 0
    paths = sorted(
        path
        for path in input_directory.rglob("*.docx")
        if "Zone.Identifier" not in path.name
    )
    for path in paths:
        relative_path = path.relative_to(input_directory).as_posix()
        document_bytes = path.stat().st_size
        total_bytes += document_bytes
        elements, parser_audit = parser.parse_with_audit(
            path,
            display_path=relative_path,
        )
        counts = Counter(element.kind.value for element in elements)
        unique_media.update(
            element.content_sha256
            for element in elements
            if element.kind.value == "image"
        )
        totals.update(counts)
        totals["blank_text_elements"] += sum(
            element.kind.value != "image" and not element.text.strip()
            for element in elements
        )
        automatic_numbering = sum(
            element.kind.value == "paragraph"
            and element.list_level is not None
            for element in elements
        )
        totals["automatic_numbering_paragraphs_detected"] += (
            automatic_numbering
        )
        totals["automatic_numbering_markers_not_represented"] += (
            automatic_numbering
        )
        totals["toc_controls_skipped"] += (
            parser_audit.toc_controls_skipped
        )
        totals["ordinary_controls_parsed"] += (
            parser_audit.ordinary_controls_parsed
        )
        totals["unsupported_nodes"] += parser_audit.unsupported_nodes
        totals["unsupported_content_with_evidence"] += (
            parser_audit.unsupported_content_with_evidence
        )
    return {
        "documents": len(paths),
        "bytes": total_bytes,
        "headings": totals["heading"],
        "paragraphs": totals["paragraph"],
        "tables": totals["table"],
        "image_references": totals["image"],
        "unique_media": len(unique_media),
        "blank_text_elements": totals["blank_text_elements"],
        "automatic_numbering_paragraphs_detected": totals[
            "automatic_numbering_paragraphs_detected"
        ],
        "automatic_numbering_markers_not_represented": totals[
            "automatic_numbering_markers_not_represented"
        ],
        "toc_controls_skipped": totals["toc_controls_skipped"],
        "ordinary_controls_parsed": totals["ordinary_controls_parsed"],
        "unsupported_nodes": totals["unsupported_nodes"],
        "unsupported_content_with_evidence": totals[
            "unsupported_content_with_evidence"
        ],
    }


def main() -> int:
    """执行命令行核对。

    Args:
        无参数。

    Returns:
        输入全部通过结构审计时返回 0，否则返回 1。

    """
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("input_directory", type=Path)
    arguments = argument_parser.parse_args()
    try:
        result = audit(arguments.input_directory)
    except _UNSAFE_DOCX_ERROR as error:
        print(
            json.dumps(
                {"error_type": type(error).__name__},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
