from __future__ import annotations

from pathlib import Path

import pytest

from scripts.offline_bundle import publish_directory


def test_publish_directory_is_atomic_and_preserves_contents(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".release-stage"
    source.mkdir()
    (source / "complete.txt").write_text("complete", encoding="utf-8")
    destination = tmp_path / "release"

    publish_directory(source, destination)

    assert not source.exists()
    assert (destination / "complete.txt").read_text(
        encoding="utf-8"
    ) == "complete"


@pytest.mark.parametrize("existing_kind", ("file", "empty-dir", "full-dir"))
def test_publish_directory_never_overwrites_existing_target(
    tmp_path: Path,
    existing_kind: str,
) -> None:
    source = tmp_path / ".release-stage"
    source.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    destination = tmp_path / "release"
    if existing_kind == "file":
        destination.write_text("old", encoding="utf-8")
    else:
        destination.mkdir()
        if existing_kind == "full-dir":
            (destination / "old.txt").write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError):
        publish_directory(source, destination)

    assert source.is_dir()
    if existing_kind == "file":
        assert destination.read_text(encoding="utf-8") == "old"
    elif existing_kind == "full-dir":
        assert (destination / "old.txt").read_text(
            encoding="utf-8"
        ) == "old"
    else:
        assert not tuple(destination.iterdir())
