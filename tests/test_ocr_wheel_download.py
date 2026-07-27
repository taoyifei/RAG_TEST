import hashlib
from pathlib import Path

import pytest

from scripts.download_ocr_wheels import verify_wheelhouse


def test_verify_wheelhouse_rejects_drift_and_unexpected_file(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "sample-1.0-py3-none-any.whl"
    wheel.write_bytes(b"fixed wheel")
    expected = {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()}

    verify_wheelhouse(tmp_path, expected)

    wheel.write_bytes(b"drift")
    with pytest.raises(ValueError, match="SHA256"):
        verify_wheelhouse(tmp_path, expected)

    wheel.write_bytes(b"fixed wheel")
    (tmp_path / "unexpected.whl").write_bytes(b"extra")
    with pytest.raises(ValueError, match="文件集合"):
        verify_wheelhouse(tmp_path, expected)
