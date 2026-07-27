from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scripts.check_release_safety import scan_repository

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


def test_release_scan_accepts_text_only_public_candidate(
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


def test_release_scan_rejects_every_private_artifact_class(
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


def test_release_scan_rejects_quoted_literal_secret(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "literal-secret"
    repository.mkdir()
    _initialize_repository(repository)
    (repository / "settings.py").write_text(
        "api_" + 'key="committed-secret"\n',
        encoding="utf-8",
    )
    _stage_all(repository)

    report = scan_repository(repository)

    assert not report.passed
    assert report.as_dict()["secret_matches"] == 1
