from __future__ import annotations

from pathlib import Path

from tests.test_package_transaction import _prepare_sandbox, _run_package


def test_package_rejects_app_image_from_old_revision(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_package(sandbox, FAKE_IMAGE_REVISION="0" * 40)

    assert completed.returncode != 0
    assert "revision" in completed.stderr
