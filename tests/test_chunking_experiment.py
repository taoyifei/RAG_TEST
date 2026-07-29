import os
import subprocess
import sys
from pathlib import Path

from evaluation.chunking_experiment import summarize_token_lengths
from evaluation.legacy_chunking import (
    fixed_token_windows,
    legacy_element_chunks,
)
from rag_app.chunking import ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import Element, ElementKind, Locator


def test_script_help_runs_outside_repository(tmp_path: Path) -> None:
    """实验脚本应能从仓库外直接启动。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        无返回值。

    """
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(repository / "evaluation" / "chunking_experiment.py"),
            "--help",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _element(
    *,
    element_id: str,
    text: str,
    kind: ElementKind,
    paragraph_index: int | None = None,
    heading_index: int | None = None,
) -> Element:
    """构造实验用最小文档元素。

    Args:
        element_id: 稳定元素 ID。
        text: 原始元素文本。
        kind: 元素类型。
        paragraph_index: 可选段落序号。
        heading_index: 可选标题序号。

    Returns:
        可供 legacy 实验消费的元素。

    """
    return Element(
        element_id=element_id,
        kind=kind,
        text=text,
        locator=Locator(
            file_path="private-name.docx",
            heading_path=("private-heading",),
            heading_index=heading_index,
            paragraph_index=paragraph_index,
            fragment=text[:240],
        ),
        content_sha256="a" * 64,
    )


def test_summarize_token_lengths_reports_tail_and_maximum() -> None:
    summary = summarize_token_lengths([1, 2, 2, 4, 9])

    assert summary == {
        "count": 5,
        "minimum": 1,
        "p50": 2,
        "p90": 9,
        "p95": 9,
        "maximum": 9,
    }


def test_fixed_baseline_builds_real_windows_with_source_ranges() -> None:
    """固定基线必须生成真实文本窗口与可复核来源范围。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    elements = (
        _element(
            element_id="element-1",
            text="a" * 400,
            kind=ElementKind.PARAGRAPH,
            paragraph_index=1,
        ),
        _element(
            element_id="element-2",
            text="b" * 200,
            kind=ElementKind.PARAGRAPH,
            paragraph_index=2,
        ),
    )

    windows = fixed_token_windows(
        elements,
        Utf8TokenCounter(),
        window_tokens=512,
    )

    assert [len(window.text) for window in windows] == [512, 89]
    assert "".join(window.text for window in windows) == (
        f"{elements[0].text}\n{elements[1].text}"
    )
    assert windows[0].source_start_char == 0
    assert windows[0].source_end_char == 512
    assert windows[1].source_start_char == 512
    assert windows[1].source_end_char == 601
    assert windows[0].locators == (
        elements[0].locator,
        elements[1].locator,
    )


def test_legacy_element_baseline_keeps_heading_chunk() -> None:
    """冻结 legacy 基线保留旧版标题独立成块行为。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    elements = (
        _element(
            element_id="heading-1",
            text="heading",
            kind=ElementKind.HEADING,
            heading_index=1,
        ),
        _element(
            element_id="paragraph-1",
            text="paragraph",
            kind=ElementKind.PARAGRAPH,
            paragraph_index=1,
            heading_index=1,
        ),
    )

    chunks = legacy_element_chunks(
        elements,
        Utf8TokenCounter(),
        ChunkerConfig(
            target_tokens=384,
            hard_max_tokens=512,
            overlap_tokens=64,
        ),
    )

    assert [chunk.element_kind for chunk in chunks] == [
        ElementKind.HEADING,
        ElementKind.PARAGRAPH,
    ]
    assert [chunk.text for chunk in chunks] == ["heading", "paragraph"]
