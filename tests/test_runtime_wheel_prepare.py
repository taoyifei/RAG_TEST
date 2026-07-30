import json
import zipfile
from pathlib import Path

import pytest

from scripts import prepare_runtime_wheels as wheel_preparer
from scripts.prepare_runtime_wheels import verify_project_wheel

_REVISION = "a" * 40
_REVISION_MEMBER = "rag_app/_build_revision.py"
_REQUIRED_MEMBERS = (
    "rag_app/worker_runtime.py",
    "rag_app/ocr/__init__.py",
    "rag_app/ocr/main.py",
)


def _wheel(
    path: Path,
    members: tuple[str, ...],
    *,
    revision: str | None = _REVISION,
) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for member in members:
            archive.writestr(member, "content")
        if revision is not None:
            archive.writestr(
                _REVISION_MEMBER,
                f'SOURCE_REVISION = "{revision}"\n',
            )


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
        _REQUIRED_MEMBERS,
    )

    assert verify_project_wheel(wheel, expected_revision=_REVISION) == _REVISION


def test_project_wheel_requires_build_revision(tmp_path: Path) -> None:
    wheel = tmp_path / "docx_rag-0.1.0-py3-none-any.whl"
    _wheel(
        wheel,
        _REQUIRED_MEMBERS,
        revision=None,
    )

    with pytest.raises(ValueError, match="revision"):
        verify_project_wheel(wheel)


@pytest.mark.parametrize("revision", ("development-unset", "A" * 40))
def test_project_wheel_rejects_invalid_revision(
    tmp_path: Path,
    revision: str,
) -> None:
    wheel = tmp_path / "docx_rag-0.1.0-py3-none-any.whl"
    _wheel(wheel, _REQUIRED_MEMBERS, revision=revision)

    with pytest.raises(ValueError, match="revision"):
        verify_project_wheel(wheel)


def test_project_wheel_revision_must_match_expected_head(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "docx_rag-0.1.0-py3-none-any.whl"
    _wheel(wheel, _REQUIRED_MEMBERS, revision=_REVISION)

    with pytest.raises(ValueError, match="HEAD"):
        verify_project_wheel(wheel, expected_revision="b" * 40)


def test_prepare_builds_from_temporary_revision_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    source_revision = repository / "src/rag_app/_build_revision.py"
    source_revision.parent.mkdir(parents=True)
    source_revision.write_text(
        'SOURCE_REVISION = "development-unset"\n',
        encoding="ascii",
    )
    tracked = [
        "src/rag_app/_build_revision.py",
        *(f"src/{relative}" for relative in _REQUIRED_MEMBERS),
    ]
    for relative in _REQUIRED_MEMBERS:
        path = repository / "src" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content", encoding="utf-8")
    lock = repository / "requirements.runtime.lock"
    lock.write_text("example-package==1.0\n", encoding="utf-8")
    tracked.append("requirements.runtime.lock")

    def git_output(_: Path, *arguments: str) -> str:
        if arguments[0] == "status":
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return _REVISION + "\n"
        if arguments == ("ls-files", "-z"):
            return "\0".join(tracked) + "\0"
        raise AssertionError(arguments)

    def build_project(source: Path, destination: Path) -> None:
        assert source != repository
        assert (
            source / "src/rag_app/_build_revision.py"
        ).read_text(encoding="ascii") == (
            f'SOURCE_REVISION = "{_REVISION}"\n'
        )
        _wheel(
            destination / "docx_rag-0.1.0-py3-none-any.whl",
            _REQUIRED_MEMBERS,
            revision=_REVISION,
        )

    monkeypatch.setattr(wheel_preparer, "_git_output", git_output)
    monkeypatch.setattr(
        wheel_preparer,
        "_download_locked_wheels",
        lambda _lock, _destination: None,
    )
    monkeypatch.setattr(
        wheel_preparer,
        "_build_project_wheel",
        build_project,
    )
    output = tmp_path / "wheelhouse"
    manifest = tmp_path / "WHEELS.sha256"
    metadata = tmp_path / "PROJECT_WHEEL.json"

    count = wheel_preparer.prepare_runtime_wheels(
        repository_root=repository,
        lock_path=lock,
        output_dir=output,
        manifest_path=manifest,
        metadata_path=metadata,
    )

    assert count == 1
    assert source_revision.read_text(encoding="ascii") == (
        'SOURCE_REVISION = "development-unset"\n'
    )
    identity = json.loads(metadata.read_text(encoding="utf-8"))
    assert identity["source_revision"] == _REVISION
    assert identity["project_wheel"].endswith(".whl")
    assert len(identity["sha256"]) == 64
    assert "docx_rag-0.1.0-py3-none-any.whl" in manifest.read_text(
        encoding="utf-8"
    )


def test_prepare_rejects_dirty_git_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    lock = repository / "requirements.runtime.lock"
    lock.write_text("example-package==1.0\n", encoding="utf-8")
    monkeypatch.setattr(
        wheel_preparer,
        "_git_output",
        lambda _root, *_arguments: " M tracked.py\n",
    )

    with pytest.raises(ValueError, match="clean Git"):
        wheel_preparer.prepare_runtime_wheels(
            repository_root=repository,
            lock_path=lock,
            output_dir=tmp_path / "wheelhouse",
            manifest_path=tmp_path / "WHEELS.sha256",
        )
