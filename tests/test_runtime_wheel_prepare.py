import zipfile
from pathlib import Path

import pytest

from scripts.prepare_runtime_wheels import verify_project_wheel


def _wheel(path: Path, members: tuple[str, ...]) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for member in members:
            archive.writestr(member, "content")


def test_project_wheel_must_contain_ocr_and_worker(
    tmp_path: Path,
) -> None:
    old_wheel = tmp_path / "docx_rag-0.1.0-py3-none-any.whl"
    _wheel(old_wheel, ("rag_app/worker_runtime.py",))

    with pytest.raises(ValueError, match="rag_app/ocr"):
        verify_project_wheel(old_wheel)


def test_current_project_wheel_contract_passes(tmp_path: Path) -> None:
    wheel = tmp_path / "docx_rag-0.1.0-py3-none-any.whl"
    _wheel(
        wheel,
        (
            "rag_app/worker_runtime.py",
            "rag_app/ocr/__init__.py",
            "rag_app/ocr/main.py",
        ),
    )

    verify_project_wheel(wheel)
