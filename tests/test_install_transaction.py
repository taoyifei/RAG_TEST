from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.freeze_corpus_manifest import freeze_corpus_manifest


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
    fakeroot_state: Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_manifest(root: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    (root / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n",
        encoding="ascii",
    )


def _write_corpus(corpus: Path, corpus_id: str, content: bytes) -> None:
    if corpus.exists():
        shutil.rmtree(corpus)
    (corpus / "docs").mkdir(parents=True)
    (corpus / "CORPUS_ID").write_text(corpus_id + "\n", encoding="ascii")
    (corpus / "docs/input.docx").write_bytes(content)
    freeze_corpus_manifest(
        docs_root=corpus / "docs",
        corpus_id=corpus_id,
        output_path=corpus / "CORPUS_MANIFEST.json",
    )
    _write_manifest(corpus)


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
    (runtime / "RELEASE_ID").write_text("release-a\n", encoding="ascii")
    (runtime / "SOURCE_REVISION").write_text("a" * 40 + "\n", encoding="ascii")
    (runtime / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (runtime / "deploy.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        project / "scripts/offline_bundle.py",
        runtime / "offline_bundle.py",
    )
    shutil.copyfile(
        project / "scripts/freeze_corpus_manifest.py",
        runtime / "freeze_corpus_manifest.py",
    )
    verify_log = tmp_path / "verify.log"
    _write_executable(
        runtime / "verify-offline.sh",
        """#!/usr/bin/env bash
set -euo pipefail
(cd "$(dirname "${BASH_SOURCE[0]}")" && sha256sum -c MANIFEST.sha256)
printf 'verified\n' >> "${FAKE_VERIFY_LOG}"
""",
    )
    _write_manifest(runtime)
    _write_corpus(corpus, "corpus-a", b"docx")
    fakeroot_state = tmp_path / "fakeroot.state"
    subprocess.run(  # noqa: S603
        [
            "/usr/bin/fakeroot",
            "-s",
            str(fakeroot_state),
            "/usr/bin/chown",
            "-R",
            "1234:1234",
            str(runtime),
            str(corpus),
        ],
        check=True,
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
    _write_executable(
        binaries / "cp",
        """#!/usr/bin/env bash
set -euo pipefail
/bin/cp "$@"
destination="${@: -1}"
if [[ "${FAKE_TAMPER_RUNTIME_STAGE:-0}" == "1" \
  && "${destination}" == */releases/.*.install.*/ ]]; then
  printf 'tampered\n' >> "${destination}/SOURCE_REVISION"
fi
if [[ "${FAKE_TAMPER_CORPUS_STAGE:-0}" == "1" \
  && "${destination}" == */shared/corpora/.*.install.*/ ]]; then
  printf 'tampered\n' >> "${destination}/docs/input.docx"
fi
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
        fakeroot_state=fakeroot_state,
    )


def _run_install(
    sandbox: _InstallSandbox,
    *,
    runtime: Path | None = None,
    corpus: Path | None = None,
    as_root: bool = True,
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
    command = [
        "/bin/bash",
        str(sandbox.script),
        str(runtime or sandbox.runtime),
        str(corpus or sandbox.corpus),
    ]
    if as_root:
        fakeroot = ["/usr/bin/fakeroot"]
        if sandbox.fakeroot_state.exists():
            fakeroot.extend(["-i", str(sandbox.fakeroot_state)])
        fakeroot.extend(["-s", str(sandbox.fakeroot_state)])
        command = [*fakeroot, *command]
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _fakeroot_stat(
    sandbox: _InstallSandbox,
    path: Path,
    *,
    format_value: str,
) -> str:
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/fakeroot",
            "-i",
            str(sandbox.fakeroot_state),
            "stat",
            "-c",
            format_value,
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _fakeroot_change(
    sandbox: _InstallSandbox,
    *arguments: str,
) -> None:
    """在同一 fakeroot 状态中修改测试文件元数据。

    Args:
        sandbox: 安装测试沙箱。
        *arguments: 传给目标系统命令的参数。

    """
    subprocess.run(  # noqa: S603
        [
            "/usr/bin/fakeroot",
            "-i",
            str(sandbox.fakeroot_state),
            "-s",
            str(sandbox.fakeroot_state),
            *arguments,
        ],
        check=True,
    )


def test_install_publishes_immutable_release_and_private_corpus(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_install(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert _fakeroot_stat(
        sandbox,
        sandbox.runtime,
        format_value="%u:%g",
    ) == "1234:1234"
    assert sandbox.verify_log.read_text(encoding="utf-8") == (
        "verified\nverified\n"
    )
    assert _fakeroot_stat(
        sandbox,
        sandbox.release_target,
        format_value="%a",
    ) == "555"
    assert _fakeroot_stat(
        sandbox,
        sandbox.release_target,
        format_value="%u:%g",
    ) == "0:0"
    for path in sandbox.release_target.rglob("*"):
        expected_mode = (
            "555"
            if path.is_dir() or path.suffix == ".sh"
            else "444"
        )
        assert _fakeroot_stat(
            sandbox,
            path,
            format_value="%u:%g",
        ) == "0:0"
        assert _fakeroot_stat(
            sandbox,
            path,
            format_value="%a",
        ) == expected_mode
    assert _fakeroot_stat(
        sandbox,
        sandbox.release_target / "compose.yaml",
        format_value="%a",
    ) == "444"
    assert _fakeroot_stat(
        sandbox,
        sandbox.release_target / "deploy.sh",
        format_value="%a",
    ) == "555"
    assert _fakeroot_stat(
        sandbox,
        sandbox.corpus_target,
        format_value="%a",
    ) == "700"
    assert _fakeroot_stat(
        sandbox,
        sandbox.corpus_target,
        format_value="%u:%g",
    ) == "10001:10001"
    assert _fakeroot_stat(
        sandbox,
        sandbox.corpus_target / "docs/input.docx",
        format_value="%a",
    ) == "400"
    assert (
        _fakeroot_stat(
            sandbox,
            sandbox.corpus_target / "docs/input.docx",
            format_value="%u:%g",
        )
        == "10001:10001"
    )
    assert sandbox.runtime.is_dir()
    assert sandbox.corpus.is_dir()
    assert not (sandbox.root / ".install.lock").exists()


@pytest.mark.parametrize(
    ("command", "expected_metadata"),
    (
        (("/usr/bin/chown", "1234:1234", "compose.yaml"), "1234:1234"),
        (("/usr/bin/chown", "1234:1234", "."), "1234:1234"),
        (("/usr/bin/chmod", "0644", "compose.yaml"), "644"),
        (("/usr/bin/chmod", "0544", "deploy.sh"), "544"),
        (("/usr/bin/chmod", "0755", "."), "755"),
    ),
)
def test_install_rejects_existing_release_owner_or_mode_drift(
    tmp_path: Path,
    command: tuple[str, str, str],
    expected_metadata: str,
) -> None:
    """证明既有 release 元数据漂移会拒绝复用且不会被修复。

    Args:
        tmp_path: pytest 临时目录。
        command: fakeroot 中执行的元数据修改命令。
        expected_metadata: 失败后仍应保留的漂移值。

    """
    sandbox = _prepare_sandbox(tmp_path)
    first = _run_install(sandbox)
    assert first.returncode == 0, first.stderr
    target = sandbox.release_target / command[2]
    _fakeroot_change(
        sandbox,
        command[0],
        command[1],
        str(target),
    )
    format_value = "%u:%g" if command[0].endswith("chown") else "%a"

    repeated = _run_install(sandbox)

    assert repeated.returncode != 0
    assert "release owner 或权限无效" in repeated.stderr
    assert _fakeroot_stat(
        sandbox,
        target,
        format_value=format_value,
    ) == expected_metadata


def test_install_requires_root_before_verification(tmp_path: Path) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_install(sandbox, as_root=False)

    assert completed.returncode != 0
    assert "root" in completed.stderr
    assert not sandbox.verify_log.exists()
    assert not sandbox.release_target.exists()
    assert not sandbox.corpus_target.exists()


def test_install_reuses_runtime_for_new_corpus_and_is_idempotent(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    first = _run_install(sandbox)
    assert first.returncode == 0, first.stderr
    release_inode = sandbox.release_target.stat().st_ino
    corpus_inode = sandbox.corpus_target.stat().st_ino
    second_corpus = tmp_path / "extracted/corpus-b"
    _write_corpus(second_corpus, "corpus-b", b"second")

    second = _run_install(sandbox, corpus=second_corpus)
    repeated = _run_install(sandbox)

    assert second.returncode == 0, second.stderr
    assert repeated.returncode == 0, repeated.stderr
    assert sandbox.release_target.stat().st_ino == release_inode
    assert sandbox.corpus_target.stat().st_ino == corpus_inode
    assert (sandbox.root / "shared/corpora/corpus-b").is_dir()
    assert _fakeroot_stat(
        sandbox,
        sandbox.root / "shared/corpora/corpus-b",
        format_value="%u:%g",
    ) == "10001:10001"


def test_install_rejects_existing_runtime_and_corpus_drift(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    first = _run_install(sandbox)
    assert first.returncode == 0, first.stderr
    release_manifest = (
        sandbox.release_target / "MANIFEST.sha256"
    ).read_bytes()
    corpus_manifest = (
        sandbox.corpus_target / "MANIFEST.sha256"
    ).read_bytes()
    (sandbox.runtime / "compose.yaml").write_text(
        "services: {changed: {}}\n",
        encoding="utf-8",
    )
    _write_manifest(sandbox.runtime)

    runtime_drift = _run_install(sandbox)

    assert runtime_drift.returncode != 0
    assert (
        sandbox.release_target / "MANIFEST.sha256"
    ).read_bytes() == release_manifest
    _write_manifest(sandbox.runtime)
    shutil.copyfile(
        sandbox.release_target / "compose.yaml",
        sandbox.runtime / "compose.yaml",
    )
    _write_manifest(sandbox.runtime)
    _write_corpus(sandbox.corpus, "corpus-a", b"changed")

    corpus_drift = _run_install(sandbox)

    assert corpus_drift.returncode != 0
    assert (
        sandbox.corpus_target / "MANIFEST.sha256"
    ).read_bytes() == corpus_manifest


def test_install_reverifies_copied_runtime_and_corpus(
    tmp_path: Path,
) -> None:
    runtime = _prepare_sandbox(tmp_path / "runtime")

    runtime_result = _run_install(
        runtime,
        FAKE_TAMPER_RUNTIME_STAGE="1",
    )

    assert runtime_result.returncode != 0
    assert not runtime.release_target.exists()
    assert not runtime.corpus_target.exists()
    corpus = _prepare_sandbox(tmp_path / "corpus")

    corpus_result = _run_install(
        corpus,
        FAKE_TAMPER_CORPUS_STAGE="1",
    )

    assert corpus_result.returncode != 0
    assert not corpus.release_target.exists()
    assert not corpus.corpus_target.exists()


def test_install_rejects_semantically_invalid_corpus_manifest(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    manifest_path = sandbox.corpus / "CORPUS_MANIFEST.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    _write_manifest(sandbox.corpus)

    completed = _run_install(sandbox)

    assert completed.returncode != 0
    assert not sandbox.release_target.exists()
    assert not sandbox.corpus_target.exists()


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
