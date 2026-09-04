import json
import zipfile
from pathlib import Path

import pytest

from scripts import prepare_runtime_wheels as wheel_preparer
from scripts.prepare_runtime_wheels import verify_project_wheel

_REVISION = "a" * 40
_REVISION_MEMBER = "rag_app/_build_revision.py"
_REQUIRED_MEMBERS = (
    "rag_app/api/product.py",
    "rag_app/composition/product_runtime.py",
    "rag_app/worker_runtime.py",
    "rag_app/ocr/__init__.py",
    "rag_app/ocr/main.py",
    "rag_app/product/crypto.py",
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


def _repository(tmp_path: Path) -> tuple[Path, Path, list[str]]:
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
    return repository, lock, tracked


def _mock_git(
    monkeypatch: pytest.MonkeyPatch,
    tracked: list[str],
) -> None:
    def git_output(_: Path, *arguments: str) -> str:
        if arguments[0] == "status":
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return _REVISION + "\n"
        if arguments == ("ls-files", "-z"):
            return "\0".join(tracked) + "\0"
        raise AssertionError(arguments)

    monkeypatch.setattr(wheel_preparer, "_git_output", git_output)


def _old_artifacts(
    tmp_path: Path,
) -> tuple[Path, Path, Path, tuple[dict[str, bytes], bytes, bytes]]:
    runtime = tmp_path / "runtime"
    output = runtime / "wheelhouse"
    output.mkdir(parents=True)
    (output / "old-1.0-py3-none-any.whl").write_bytes(b"old-wheel")
    manifest = runtime / "WHEELS.sha256"
    metadata = runtime / "PROJECT_WHEEL.json"
    manifest.write_bytes(b"old manifest bytes\n")
    metadata.write_bytes(b'{"old":"metadata"}\n')
    snapshot = (
        {
            path.name: path.read_bytes()
            for path in sorted(output.iterdir())
        },
        manifest.read_bytes(),
        metadata.read_bytes(),
    )
    return output, manifest, metadata, snapshot


def _artifact_snapshot(
    output: Path,
    manifest: Path,
    metadata: Path,
) -> tuple[dict[str, bytes], bytes, bytes]:
    return (
        {
            path.name: path.read_bytes()
            for path in sorted(output.iterdir())
        },
        manifest.read_bytes(),
        metadata.read_bytes(),
    )


def test_project_wheel_must_contain_all_runtime_roots(
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
    repository, lock, tracked = _repository(tmp_path)
    source_revision = repository / "src/rag_app/_build_revision.py"

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

    _mock_git(monkeypatch, tracked)
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
    assert not tuple(tmp_path.glob(".runtime-wheels-*"))


def test_prepare_replaces_old_three_artifacts_as_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, lock, tracked = _repository(tmp_path)
    _mock_git(monkeypatch, tracked)
    output, manifest, metadata, old_snapshot = _old_artifacts(tmp_path)

    def download(_lock: Path, destination: Path) -> None:
        (destination / "dependency-1.0-py3-none-any.whl").write_bytes(
            b"dependency"
        )

    def build(_source: Path, destination: Path) -> None:
        _wheel(
            destination / "docx_rag-0.1.0-py3-none-any.whl",
            _REQUIRED_MEMBERS,
            revision=_REVISION,
        )

    monkeypatch.setattr(wheel_preparer, "_download_locked_wheels", download)
    monkeypatch.setattr(wheel_preparer, "_build_project_wheel", build)

    count = wheel_preparer.prepare_runtime_wheels(
        repository_root=repository,
        lock_path=lock,
        output_dir=output,
        manifest_path=manifest,
        metadata_path=metadata,
    )

    assert count == 2
    assert _artifact_snapshot(output, manifest, metadata) != old_snapshot
    identity = json.loads(metadata.read_text(encoding="utf-8"))
    assert identity["source_revision"] == _REVISION
    assert {path.name for path in output.iterdir()} == {
        "dependency-1.0-py3-none-any.whl",
        "docx_rag-0.1.0-py3-none-any.whl",
    }
    assert not tuple((tmp_path / "runtime").glob(".runtime-wheels-*"))


@pytest.mark.parametrize(
    "failure",
    (
        "download",
        "build",
        "unexpected",
        "manifest",
        "metadata",
        "move",
    ),
)
def test_prepare_failure_keeps_old_three_artifacts_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository, lock, tracked = _repository(tmp_path)
    _mock_git(monkeypatch, tracked)
    output, manifest, metadata, old_snapshot = _old_artifacts(tmp_path)

    def download(_lock: Path, destination: Path) -> None:
        if failure == "download":
            raise RuntimeError("synthetic download failure")
        (destination / "dependency-1.0-py3-none-any.whl").write_bytes(
            b"dependency"
        )
        if failure == "unexpected":
            (destination / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )

    def build(_source: Path, destination: Path) -> None:
        if failure == "build":
            raise RuntimeError("synthetic build failure")
        _wheel(
            destination / "docx_rag-0.1.0-py3-none-any.whl",
            _REQUIRED_MEMBERS,
            revision=_REVISION,
        )

    monkeypatch.setattr(wheel_preparer, "_download_locked_wheels", download)
    monkeypatch.setattr(wheel_preparer, "_build_project_wheel", build)
    original_write_text = Path.write_text

    def write_text(
        path: Path,
        content: str,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if (
            failure == "manifest"
            and path.name == "WHEELS.sha256"
        ) or (
            failure == "metadata"
            and path.name == "PROJECT_WHEEL.json"
        ):
            raise OSError(f"synthetic {failure} write failure")
        return original_write_text(
            path,
            content,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", write_text)
    replace_calls = 0

    def replace_path(source: Path, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if failure == "move" and replace_calls == 4:
            raise OSError("synthetic move failure")
        source.replace(destination)

    monkeypatch.setattr(
        wheel_preparer,
        "_replace_path",
        replace_path,
        raising=False,
    )

    with pytest.raises((OSError, RuntimeError, ValueError)):
        wheel_preparer.prepare_runtime_wheels(
            repository_root=repository,
            lock_path=lock,
            output_dir=output,
            manifest_path=manifest,
            metadata_path=metadata,
        )

    assert _artifact_snapshot(output, manifest, metadata) == old_snapshot


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


def test_download_uses_target_compatible_linux_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = tmp_path / "requirements.runtime.lock"
    lock.write_text("grpcio==1.83.0\n", encoding="utf-8")
    destination = tmp_path / "wheelhouse"
    destination.mkdir()
    captured: list[str] = []

    def run(command: list[str], *, check: bool) -> None:
        assert check is True
        captured.extend(command)

    monkeypatch.setattr(wheel_preparer.subprocess, "run", run)

    wheel_preparer._download_locked_wheels(lock, destination)

    platforms = tuple(
        captured[index + 1]
        for index, argument in enumerate(captured)
        if argument == "--platform"
    )
    abis = tuple(
        captured[index + 1]
        for index, argument in enumerate(captured)
        if argument == "--abi"
    )
    assert platforms == (
        "manylinux_2_28_x86_64",
        "manylinux_2_17_x86_64",
        "manylinux2014_x86_64",
    )
    assert abis == ("cp311", "abi3")
    assert "--only-binary=:all:" in captured
