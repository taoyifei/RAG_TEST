from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _InstallSandbox:
    script: Path
    runtime: Path
    corpus: Path
    root: Path
    release_target: Path
    corpus_target: Path
    binaries: Path
    verify_log: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _prepare_sandbox(tmp_path: Path) -> _InstallSandbox:
    project = Path(__file__).parents[1]
    root = tmp_path / "RAG"
    for directory in (
        root,
        root / "releases",
        root / "shared/corpora",
        root / "shared/env",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    extracted = tmp_path / "extracted"
    runtime = extracted / "runtime"
    corpus = extracted / "corpus"
    runtime.mkdir(parents=True)
    (corpus / "docs").mkdir(parents=True)
    (runtime / "RELEASE_ID").write_text("release-a\n", encoding="ascii")
    (runtime / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (runtime / "deploy.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        project / "scripts/offline_bundle.py",
        runtime / "offline_bundle.py",
    )
    verify_log = tmp_path / "verify.log"
    _write_executable(
        runtime / "verify-offline.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'verified\n' > "${FAKE_VERIFY_LOG}"
""",
    )
    (corpus / "CORPUS_ID").write_text("corpus-a\n", encoding="ascii")
    (corpus / "docs/input.docx").write_bytes(b"docx")
    manifest_lines = []
    for path in (
        corpus / "CORPUS_ID",
        corpus / "docs/input.docx",
    ):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_lines.append(
            f"{digest}  {path.relative_to(corpus).as_posix()}"
        )
    (corpus / "MANIFEST.sha256").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="ascii",
    )
    script = tmp_path / "install.sh"
    source = (project / "deployment/install.sh").read_text(encoding="utf-8")
    script.write_text(
        source.replace(
            'project_root="/data/tyf/RAG"',
            f'project_root="{root}"',
            1,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_executable(
        binaries / "python3",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{FAKE_RELEASE_RACE:-0}}" == "1" \
  && "${{@: -1}}" == */releases/release-a ]]; then
  destination="${{@: -1}}"
  mkdir -p "${{destination}}"
  printf 'racer\n' > "${{destination}}/owner.txt"
fi
exec "{sys.executable}" "$@"
""",
    )
    return _InstallSandbox(
        script=script,
        runtime=runtime,
        corpus=corpus,
        root=root,
        release_target=root / "releases/release-a",
        corpus_target=root / "shared/corpora/corpus-a",
        binaries=binaries,
        verify_log=verify_log,
    )


def _run_install(
    sandbox: _InstallSandbox,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_VERIFY_LOG": str(sandbox.verify_log),
            "PATH": f"{sandbox.binaries}:/usr/bin:/bin",
        }
    )
    environment.update(overrides)
    return subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(sandbox.script),
            str(sandbox.runtime),
            str(sandbox.corpus),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_install_publishes_immutable_release_and_private_corpus(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_install(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert sandbox.verify_log.read_text(encoding="utf-8") == "verified\n"
    assert stat.S_IMODE(sandbox.release_target.stat().st_mode) == 0o555
    assert (
        stat.S_IMODE(
            (sandbox.release_target / "compose.yaml").stat().st_mode
        )
        == 0o444
    )
    assert (
        stat.S_IMODE(
            (sandbox.release_target / "deploy.sh").stat().st_mode
        )
        == 0o555
    )
    assert stat.S_IMODE(sandbox.corpus_target.stat().st_mode) == 0o700
    assert (
        stat.S_IMODE(
            (
                sandbox.corpus_target / "docs/input.docx"
            ).stat().st_mode
        )
        == 0o400
    )
    assert sandbox.runtime.is_dir()
    assert sandbox.corpus.is_dir()
    assert not (sandbox.root / ".install.lock").exists()


def test_install_rejects_existing_target_and_unsafe_shared_env(
    tmp_path: Path,
) -> None:
    existing = _prepare_sandbox(tmp_path / "existing")
    existing.release_target.mkdir()
    marker = existing.release_target / "owner.txt"
    marker.write_text("existing", encoding="utf-8")

    existing_result = _run_install(existing)

    assert existing_result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "existing"
    unsafe = _prepare_sandbox(tmp_path / "unsafe")
    env_file = unsafe.root / "shared/env/rag.env"
    env_file.write_text(
        "TOKEN=REPLACE_TEST_VALUE\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    unsafe_result = _run_install(unsafe)

    assert unsafe_result.returncode != 0
    assert not unsafe.release_target.exists()
    assert not unsafe.corpus_target.exists()


def test_install_rejects_secret_env_inside_release_input(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    (sandbox.runtime / "secrets.env").write_text(
        "TOKEN=REPLACE_TEST_VALUE\n",
        encoding="utf-8",
    )

    completed = _run_install(sandbox)

    assert completed.returncode != 0
    assert not sandbox.release_target.exists()
    assert not sandbox.corpus_target.exists()


def test_release_rename_race_removes_own_corpus_and_preserves_racer(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_install(sandbox, FAKE_RELEASE_RACE="1")

    assert completed.returncode != 0
    assert not sandbox.corpus_target.exists()
    assert (sandbox.release_target / "owner.txt").read_text(
        encoding="utf-8"
    ) == "racer\n"
    assert not (sandbox.release_target / "compose.yaml").exists()
    assert not tuple(
        (sandbox.root / "releases").glob(".release-a.install.*")
    )
    assert not tuple(
        (sandbox.root / "shared/corpora").glob(".corpus-a.install.*")
    )
