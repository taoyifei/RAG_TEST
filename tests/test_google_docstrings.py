from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import check_google_docstrings


def _write_module(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_checker_reports_all_sections_for_undocumented_callable(
    tmp_path: Path,
) -> None:
    path = _write_module(
        tmp_path,
        "def undocumented(value):\n"
        "    return value\n",
    )

    findings = check_google_docstrings.audit_paths((path,))

    assert findings == (
        f"{path}:1:undocumented:Args",
        f"{path}:1:undocumented:Returns",
        f"{path}:1:undocumented:docstring",
    )


def test_checker_requires_sections_for_no_args_none_and_no_annotation(
    tmp_path: Path,
) -> None:
    path = _write_module(
        tmp_path,
        'def no_args() -> None:\n'
        '    """只有摘要。"""\n'
        "\n"
        "\n"
        "def no_annotation(value):\n"
        '    """只有摘要。\n'
        "\n"
        "    Args:\n"
        "        value: 中文参数。\n"
        '    """\n'
        "    return value\n",
    )

    findings = check_google_docstrings.audit_paths((path,))

    assert findings == (
        f"{path}:1:no_args:Args",
        f"{path}:1:no_args:Returns",
        f"{path}:5:no_annotation:Returns",
    )


def test_checker_requires_chinese_sections_and_nested_callable(
    tmp_path: Path,
) -> None:
    path = _write_module(
        tmp_path,
        'def outer(value: str) -> str:\n'
        '    """外层说明。\n'
        "\n"
        "    Args:\n"
        "        value: 中文参数。\n"
        "\n"
        "    Returns:\n"
        "        中文结果。\n"
        '    """\n'
        "    def nested() -> None:\n"
        '        """Nested summary.\n'
        "\n"
        "        Args:\n"
        "            No arguments.\n"
        "\n"
        "        Returns:\n"
        "            None.\n"
        '        """\n'
        "        return None\n"
        "    nested()\n"
        "    return value\n",
    )

    findings = check_google_docstrings.audit_paths((path,))

    assert findings == (
        f"{path}:10:nested:Args",
        f"{path}:10:nested:Returns",
    )


def test_default_cli_audits_full_roots(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[tuple[Path, ...]] = []
    monkeypatch.setattr(
        check_google_docstrings,
        "audit_paths",
        lambda paths: captured.append(tuple(paths)) or (),
    )
    monkeypatch.setattr(
        check_google_docstrings,
        "_changed_python_files",
        lambda _: pytest.fail("默认命令不得缩小为 changed 文件。"),
    )
    monkeypatch.setattr(sys, "argv", ["check_google_docstrings.py"])

    assert check_google_docstrings.main() == 0
    assert captured == [
        (
            Path("src/rag_app"),
            Path("evaluation"),
            Path("scripts"),
        )
    ]
    assert capsys.readouterr().out == "missing_google_sections=0\n"


def test_changed_cli_is_the_only_changed_file_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = (Path("src/rag_app/example.py"),)
    captured: list[tuple[Path, ...]] = []
    monkeypatch.setattr(
        check_google_docstrings,
        "_changed_python_files",
        lambda _: changed,
    )
    monkeypatch.setattr(
        check_google_docstrings,
        "audit_paths",
        lambda paths: captured.append(tuple(paths)) or (),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_google_docstrings.py", "--changed"],
    )

    assert check_google_docstrings.main() == 0
    assert captured == [changed]
