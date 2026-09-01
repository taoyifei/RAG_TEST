from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.check_release_safety import (
    ReleaseSafetyError,
    main,
    scan_release,
    scan_repository,
    scan_staged,
)
from scripts.freeze_corpus_manifest import freeze_corpus_manifest

_GIT_EXECUTABLE = shutil.which("git")


def _initialize_repository(path: Path) -> None:
    assert _GIT_EXECUTABLE is not None
    subprocess.run(  # noqa: S603
        [_GIT_EXECUTABLE, "init", "--quiet", str(path)],
        check=True,
    )


def _stage_all(path: Path) -> None:
    assert _GIT_EXECUTABLE is not None
    subprocess.run(  # noqa: S603
        [_GIT_EXECUTABLE, "-C", str(path), "add", "--all"],
        check=True,
    )


def _commit_all(path: Path) -> None:
    assert _GIT_EXECUTABLE is not None
    for key, value in (
        ("user.name", "Release Safety Test"),
        ("user.email", "release-safety@example.invalid"),
    ):
        subprocess.run(  # noqa: S603
            [_GIT_EXECUTABLE, "-C", str(path), "config", key, value],
            check=True,
        )
    subprocess.run(  # noqa: S603
        [_GIT_EXECUTABLE, "-C", str(path), "commit", "--quiet", "-m", "base"],
        check=True,
    )


def _tracked_count(path: Path) -> int:
    assert _GIT_EXECUTABLE is not None
    completed = subprocess.run(  # noqa: S603
        [_GIT_EXECUTABLE, "-C", str(path), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return len(completed.stdout.splitlines())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(root: Path, name: str) -> None:
    manifest = root / name
    rows = [
        f"{_sha256(path)}  ./{path.relative_to(root).as_posix()}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != manifest
    ]
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _release_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    delivery = tmp_path / "delivery"
    runtime = tmp_path / "unpacked-runtime" / "runtime"
    corpus = tmp_path / "unpacked-corpus" / "corpus"
    images = runtime / "images"
    docs = corpus / "docs"
    delivery.mkdir()
    images.mkdir(parents=True)
    docs.mkdir(parents=True)

    runtime_archive = delivery / "rag-runtime-release-1.tar.gz"
    corpus_archive = delivery / "rag-corpus-corpus-1.tar.gz"
    unpacker = delivery / "offline_bundle.py"
    runtime_archive.write_bytes(b"\x1f\x8b\x08runtime")
    corpus_archive.write_bytes(b"\x1f\x8b\x08corpus")
    unpacker.write_text("print('safe unpacker')\n", encoding="utf-8")
    for path in (runtime_archive, corpus_archive, unpacker):
        path.with_name(f"{path.name}.sha256").write_text(
            f"{_sha256(path)}  {path.name}\n",
            encoding="ascii",
        )

    (runtime / "RELEASE_METADATA.json").write_text(
        json.dumps(
            {
                "configuration_status": "provisional",
                "release_tier": "smoke",
                "schema_version": "1",
                "source_revision": "a" * 40,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for name in (
        "docx-rag-linux-amd64.tar",
        "docx-rag-ocr-linux-amd64.tar",
        "qdrant-linux-amd64.tar",
    ):
        (images / name).write_bytes(b"archive\x00payload")

    (docs / "sample.docx").write_bytes(b"PK\x03\x04verified-docx")
    freeze_corpus_manifest(
        docs_root=docs,
        corpus_id="corpus-1",
        output_path=corpus / "CORPUS_MANIFEST.json",
    )
    (corpus / "CORPUS_ID").write_text("corpus-1\n", encoding="ascii")

    _write_manifest(runtime, "MANIFEST.sha256")
    _write_manifest(corpus, "MANIFEST.sha256")
    _write_manifest(delivery, "RELEASE_MANIFEST.sha256")
    return delivery, runtime, corpus


def test_repository_mode_reports_current_existing_violations() -> None:
    repository = Path(__file__).parents[1]

    report = scan_repository(repository)
    payload = report.as_dict()

    assert payload["mode"] == "repository"
    assert payload["tracked_files"] == _tracked_count(repository)
    assert payload["violations"] == sum(
        int(payload[field])
        for field in (
            "binary_files",
            "integrity_errors",
            "large_files",
            "local_path_matches",
            "private_network_matches",
            "private_paths",
            "secret_matches",
        )
    )
    assert "deployment/model-services/README.md" in payload[
        "private_network_matches_details"
    ]
    assert "tests/test_model_services_deployment.py" in payload[
        "secret_matches_details"
    ]


def test_repository_scan_accepts_text_only_public_candidate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "safe"
    repository.mkdir()
    _initialize_repository(repository)
    (repository / "README.md").write_text(
        "Endpoint: http://192.0.2.10\n",
        encoding="utf-8",
    )
    (repository / ".env.example").write_text(
        "API_TOKEN=REPLACE_WITH_RANDOM_VALUE\n",
        encoding="utf-8",
    )
    _stage_all(repository)

    report = scan_repository(repository, max_file_bytes=1024)

    assert report.passed
    assert report.as_dict()["violations"] == 0


def test_repository_scan_rejects_every_private_artifact_class(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "unsafe"
    repository.mkdir()
    _initialize_repository(repository)
    (repository / "docs").mkdir()
    (repository / "docs" / "private.txt").write_text(
        "internal",
        encoding="utf-8",
    )
    (repository / "config.txt").write_text(
        "host=10." + "23.4.5\n"
        + "path=/" + "home/operator/private\n"
        + "access_key=AKIA"
        + ("A" * 16)
        + "\n",
        encoding="utf-8",
    )
    (repository / "binary.dat").write_bytes(b"prefix\x00suffix")
    (repository / "large.txt").write_text("x" * 129, encoding="utf-8")
    _stage_all(repository)

    report = scan_repository(repository, max_file_bytes=128)
    payload = report.as_dict()

    assert not report.passed
    assert payload["private_paths"] == 1
    assert payload["private_network_matches"] == 1
    assert payload["local_path_matches"] == 1
    assert payload["secret_matches"] == 1
    assert payload["binary_files"] == 1
    assert payload["large_files"] == 1


def test_staged_mode_reads_index_blob_and_accepts_legal_candidate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "staged-safe"
    repository.mkdir()
    _initialize_repository(repository)
    candidate = repository / "candidate.txt"
    candidate.write_text("public candidate\n", encoding="utf-8")
    _stage_all(repository)
    candidate.write_text(
        "host=10." + "88.0.1\n",
        encoding="utf-8",
    )

    report = scan_staged(repository)

    assert report.passed
    assert report.as_dict()["tracked_files"] == 1


def test_staged_mode_rejects_new_network_and_secret_literals(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "staged-unsafe"
    repository.mkdir()
    _initialize_repository(repository)
    (repository / "candidate.txt").write_text(
        "host=10." + "88.0.1\n" + "api_" + 'key="live-value"\n',
        encoding="utf-8",
    )
    _stage_all(repository)

    report = scan_staged(repository)

    assert not report.passed
    assert report.private_network_matches == ("candidate.txt",)
    assert report.secret_matches == ("candidate.txt",)


@pytest.mark.parametrize("kind", ("private", "binary", "large"))
def test_staged_mode_rejects_other_candidate_risk_classes(
    tmp_path: Path,
    kind: str,
) -> None:
    repository = tmp_path / f"staged-{kind}"
    repository.mkdir()
    _initialize_repository(repository)
    if kind == "private":
        candidate = repository / "docs/private.txt"
        candidate.parent.mkdir()
        candidate.write_text("private\n", encoding="utf-8")
    elif kind == "binary":
        (repository / "candidate.bin").write_bytes(b"binary\x00value")
    else:
        (repository / "candidate.txt").write_text(
            "x" * 129,
            encoding="utf-8",
        )
    _stage_all(repository)

    report = scan_staged(repository, max_file_bytes=128)

    assert not report.passed
    payload = report.as_dict()
    assert payload[
        {
            "private": "private_paths",
            "binary": "binary_files",
            "large": "large_files",
        }[kind]
    ] == 1


def test_staged_rename_scans_target_path_and_index_content(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "staged-rename"
    repository.mkdir()
    _initialize_repository(repository)
    original = repository / "original.txt"
    original.write_text("safe\n", encoding="utf-8")
    _stage_all(repository)
    _commit_all(repository)
    target = repository / "renamed.txt"
    original.rename(target)
    target.write_text("host=10." + "44.0.9\n", encoding="utf-8")
    _stage_all(repository)

    report = scan_staged(repository)

    assert report.private_network_matches == ("renamed.txt",)


def test_staged_deletion_and_empty_index_are_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "staged-empty"
    repository.mkdir()
    _initialize_repository(repository)
    candidate = repository / "candidate.txt"
    candidate.write_text("safe\n", encoding="utf-8")
    _stage_all(repository)
    _commit_all(repository)
    candidate.unlink()
    _stage_all(repository)

    assert main(["staged", str(repository)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "staged"
    assert payload["tracked_files"] == 0
    assert payload["violations"] == 0


def test_release_mode_accepts_verified_payload_and_five_archives(
    tmp_path: Path,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)

    report = scan_release(
        delivery_root=delivery,
        runtime_root=runtime,
        corpus_root=corpus,
    )

    assert report.passed
    assert report.as_dict()["mode"] == "release"
    assert report.binary_files == ()
    assert report.large_files == ()


def test_release_mode_never_materializes_approved_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.suffix.casefold() in {".docx", ".gz", ".tar"}:
            raise AssertionError("批准归档不得整包读入内存")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    report = scan_release(
        delivery_root=delivery,
        runtime_root=runtime,
        corpus_root=corpus,
    )

    assert report.passed


def test_release_mode_distinguishes_container_tool_and_user_home_paths(
    tmp_path: Path,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    candidate = runtime / "paths.txt"
    candidate.write_text(
        "tool=/home/cmake-3.18.0-Linux-x86_64/bin\n",
        encoding="utf-8",
    )
    _write_manifest(runtime, "MANIFEST.sha256")

    safe_report = scan_release(
        delivery_root=delivery,
        runtime_root=runtime,
        corpus_root=corpus,
    )

    assert safe_report.passed

    candidate.write_text(
        "private=/" + "home/operator/workspace\n",
        encoding="utf-8",
    )
    _write_manifest(runtime, "MANIFEST.sha256")
    unsafe_report = scan_release(
        delivery_root=delivery,
        runtime_root=runtime,
        corpus_root=corpus,
    )
    assert unsafe_report.local_path_matches == ("runtime/paths.txt",)


@pytest.mark.parametrize(
    "source_name",
    (
        "deployment/model-services/README.md",
        "tests/test_model_services_deployment.py",
    ),
)
def test_release_mode_rejects_existing_violation_when_registered_in_runtime(
    tmp_path: Path,
    source_name: str,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    repository = Path(__file__).parents[1]
    shutil.copyfile(repository / source_name, runtime / "registered-leak.txt")
    _write_manifest(runtime, "MANIFEST.sha256")

    report = scan_release(
        delivery_root=delivery,
        runtime_root=runtime,
        corpus_root=corpus,
    )

    assert not report.passed
    assert (
        report.private_network_matches or report.secret_matches
    ) == ("runtime/registered-leak.txt",)


def test_release_mode_rejects_unregistered_file(tmp_path: Path) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    (runtime / "unregistered.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(ReleaseSafetyError, match="exact set"):
        scan_release(
            delivery_root=delivery,
            runtime_root=runtime,
            corpus_root=corpus,
        )


def test_release_mode_rejects_docx_missing_from_corpus_manifest(
    tmp_path: Path,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    (corpus / "docs/unverified.docx").write_bytes(b"PK\x03\x04unverified")
    _write_manifest(corpus, "MANIFEST.sha256")

    with pytest.raises(ReleaseSafetyError, match="corpus manifest"):
        scan_release(
            delivery_root=delivery,
            runtime_root=runtime,
            corpus_root=corpus,
        )


def test_release_mode_rejects_manifest_digest_mismatch(tmp_path: Path) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    (runtime / "RELEASE_METADATA.json").write_text(
        "changed after manifest\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseSafetyError, match="SHA256"):
        scan_release(
            delivery_root=delivery,
            runtime_root=runtime,
            corpus_root=corpus,
        )


@pytest.mark.parametrize("root_name", ("delivery", "runtime", "corpus"))
def test_release_mode_rejects_symlink_in_any_root(
    tmp_path: Path,
    root_name: str,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    roots = {"delivery": delivery, "runtime": runtime, "corpus": corpus}
    root = roots[root_name]
    target = tmp_path / f"{root_name}-target.txt"
    target.write_text("safe\n", encoding="utf-8")
    (root / "registered-link.txt").symlink_to(target)

    with pytest.raises(ReleaseSafetyError, match="符号链接"):
        scan_release(
            delivery_root=delivery,
            runtime_root=runtime,
            corpus_root=corpus,
        )


def test_release_mode_rejects_symlink_ancestor(tmp_path: Path) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    linked_parent = tmp_path / "delivery-link"
    linked_parent.symlink_to(delivery, target_is_directory=True)

    with pytest.raises(ReleaseSafetyError, match="canonical"):
        scan_release(
            delivery_root=linked_parent,
            runtime_root=runtime,
            corpus_root=corpus,
        )


def test_release_mode_rejects_special_file(tmp_path: Path) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    os.mkfifo(runtime / "forbidden.fifo")

    with pytest.raises(ReleaseSafetyError, match="特殊文件"):
        scan_release(
            delivery_root=delivery,
            runtime_root=runtime,
            corpus_root=corpus,
        )


def test_release_mode_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    manifest = runtime / "MANIFEST.sha256"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"{'0' * 64}  ./../outside.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseSafetyError, match="路径"):
        scan_release(
            delivery_root=delivery,
            runtime_root=runtime,
            corpus_root=corpus,
        )


@pytest.mark.parametrize("kind", ("binary", "large"))
def test_release_mode_rejects_extra_binary_or_large_file(
    tmp_path: Path,
    kind: str,
) -> None:
    delivery, runtime, corpus = _release_fixture(tmp_path)
    if kind == "binary":
        (runtime / "extra.bin").write_bytes(b"unexpected\x00binary")
    else:
        (runtime / "extra.txt").write_text("x" * 10_001, encoding="utf-8")
    _write_manifest(runtime, "MANIFEST.sha256")

    report = scan_release(
        delivery_root=delivery,
        runtime_root=runtime,
        corpus_root=corpus,
        max_file_bytes=10_000,
    )

    assert not report.passed
    if kind == "binary":
        assert report.binary_files == ("runtime/extra.bin",)
    else:
        assert report.large_files == ("runtime/extra.txt",)
