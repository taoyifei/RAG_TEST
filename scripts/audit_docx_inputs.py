"""核对冻结 DOCX 输入的解析结果。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from rag_app.contracts import ElementKind
from rag_app.parsers.docx import DocxParser


def audit(input_directory: Path) -> dict[str, object]:
    """解析目录中的全部 DOCX 并汇总元素数量。

    Args:
        input_directory: 冻结输入目录。

    Returns:
        包含逐文档和总计数的 JSON 兼容字典。

    """
    parser = DocxParser()
    documents: list[dict[str, object]] = []
    totals: Counter[str] = Counter()
    media_entries: set[tuple[str, str]] = set()
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
        elements = parser.parse(path, display_path=relative_path)
        counts = Counter(element.kind.value for element in elements)
        media_entries.update(
            (relative_path, element.media_name)
            for element in elements
            if element.media_name is not None
        )
        totals.update(counts)
        documents.append(
            {
                "path": relative_path,
                "bytes": document_bytes,
                "elements": len(elements),
                "headings": counts[ElementKind.HEADING.value],
                "paragraphs": counts[ElementKind.PARAGRAPH.value],
                "tables": counts[ElementKind.TABLE.value],
                "images": counts[ElementKind.IMAGE.value],
            }
        )
    return {
        "documents": documents,
        "totals": {
            "documents": len(documents),
            "bytes": total_bytes,
            "headings": totals[ElementKind.HEADING.value],
            "paragraphs": totals[ElementKind.PARAGRAPH.value],
            "tables": totals[ElementKind.TABLE.value],
            "images": totals[ElementKind.IMAGE.value],
            "unique_media_entries": len(media_entries),
        },
    }


def main() -> None:
    """执行命令行核对。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("input_directory", type=Path)
    arguments = argument_parser.parse_args()
    result = audit(arguments.input_directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
