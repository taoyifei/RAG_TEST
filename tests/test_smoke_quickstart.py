"""验证唯一 smoke 操作文档的最短主路径。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release_smoke import SmokeContext, _write_report

_REPORT_READER_BEGIN = "# quickstart-report-reader-begin"
_REPORT_READER_END = "# quickstart-report-reader-end"
_FAKEROOT = "/usr/bin/fakeroot"


def _quickstart_report_reader() -> str:
    """提取 Quickstart 实际执行的报告字段读取器。"""
    quickstart = Path(__file__).parents[1] / "deployment/README.md"
    content = quickstart.read_text(encoding="utf-8")
    begin = content.index(_REPORT_READER_BEGIN)
    end = content.index(_REPORT_READER_END, begin)
    return content[begin:end].strip()


def _write_report_fixture(tmp_path: Path) -> Path:
    """使用生产报告写入器生成行为测试输入。"""
    report_path = tmp_path / "release-smoke-report.json"
    context = SmokeContext(
        root=tmp_path,
        report_path=report_path,
        head="b" * 40,
        release_id="release-2",
        corpus_id="corpus-2",
        release_dir=tmp_path / "release",
        files=[
            {"name": str(index), "sha256": f"{index:064x}", "size_bytes": 1}
            for index in range(7)
        ],
    )
    _write_report(context, {"stages": [], "status": "passed"})
    return report_path


def _run_report_reader(report_path: Path) -> subprocess.CompletedProcess[str]:
    """对指定真实 schema 报告执行 Quickstart 字段读取器。"""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-", str(report_path)],
        input=_quickstart_report_reader(),
        check=False,
        capture_output=True,
        text=True,
    )


def _run_bootstrap(
    project_root: Path,
    state_path: Path,
) -> subprocess.CompletedProcess[str]:
    """在持久 fakeroot 身份库中执行真实 bootstrap。"""
    command = [_FAKEROOT]
    if state_path.exists():
        command.extend(("-i", str(state_path)))
    command.extend(
        (
            "-s",
            str(state_path),
            "--",
            "/bin/bash",
            str(Path(__file__).parents[1] / "deployment/bootstrap.sh"),
            str(project_root),
        )
    )
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def _fake_stat(state_path: Path, paths: tuple[Path, ...]) -> list[str]:
    """从指定 fakeroot 身份库读取 owner、group 与 mode。"""
    completed = subprocess.run(  # noqa: S603
        [
            _FAKEROOT,
            "-i",
            str(state_path),
            "--",
            "/usr/bin/stat",
            "-c",
            "%u:%g:%a",
            *map(str, paths),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.splitlines()


def test_smoke_quickstart_is_short_and_orders_single_primary_path() -> None:
    """锁定 200 行内的七文件 smoke 发布顺序。"""
    root = Path(__file__).parents[1]
    quickstart = root / "deployment/README.md"
    content = quickstart.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert len(lines) <= 200
    ordered_markers = (
        "## 1. 本地 preflight",
        "scripts/release_smoke.py",
        "## 2. 上传恰好七个文件",
        "## 3. 服务器校验、解包与 server-preflight.sh",
        'server-preflight.sh" "${runtime_dir}" - fresh',
        'bash "${runtime_dir}/bootstrap.sh" /data/tyf/RAG',
        "## 4. install 与 deploy",
        'bash "${runtime_dir}/server-preflight.sh" \\\n'
        '  "${runtime_dir}" "${candidate}" fresh',
        'bash "${release_dir}/deploy.sh"',
        "## 5. 冒烟验证",
    )
    positions = [content.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert "Docker Engine 29" not in content
    assert "containerd image store" not in content
    assert 'cat "${release_dir}/RELEASE_ID"' not in content
    assert 'cat "${release_dir}/SOURCE_REVISION"' not in content
    assert 'cat "${runtime_dir}/RELEASE_ID"' in content
    assert 'cat "${runtime_dir}/SOURCE_REVISION"' in content
    assert "rsync --partial --append-verify" in content
    assert "SHA256 仍是完整性交付的唯一权威" in content
    assert "RAG_QDRANT_API_KEY" in content
    assert "'api-key'" in content
    assert "RUN/selfcheck 网络隔离" in content
    assert "不代表 frontend 完全断网" in content
    assert "smoke 成功后才清理本次 incoming/extracted" in content
    assert "保留 active 与 rollback release" in content
    assert "完整参考" in content
    assert "design/public/offline-build-and-server-deployment.md" in content


def test_quickstart_reads_all_real_report_identity_fields(
    tmp_path: Path,
) -> None:
    """证明 Quickstart 读取器可消费真实报告 schema。"""
    report_path = _write_report_fixture(tmp_path)

    completed = _run_report_reader(report_path)

    assert completed.returncode == 0, completed.stderr
    assignments = dict(
        line.split("=", maxsplit=1) for line in completed.stdout.splitlines()
    )
    assert assignments == {
        "corpus_id": "corpus-2",
        "release_dir": str(tmp_path / "release"),
        "release_id": "release-2",
        "source_revision": "b" * 40,
    }


def test_quickstart_rejects_report_missing_required_field(
    tmp_path: Path,
) -> None:
    """证明报告缺任一身份字段时 Quickstart 立即失败。"""
    report_path = _write_report_fixture(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    del report["source_revision"]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    completed = _run_report_reader(report_path)

    assert completed.returncode != 0
    assert "missing report fields: source_revision" in completed.stderr


def test_bootstrap_rejects_non_root_without_changes(tmp_path: Path) -> None:
    """证明普通用户不能创建 fresh 目录。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    script = Path(__file__).parents[1] / "deployment/bootstrap.sh"

    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", str(script), str(project_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "root" in completed.stderr
    assert not tuple(project_root.iterdir())


def test_bootstrap_is_idempotent_with_exact_owners_and_modes(
    tmp_path: Path,
) -> None:
    """证明 fresh 创建、精确权限和重复执行均符合契约。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    state_path = tmp_path / "fakeroot.state"

    first = _run_bootstrap(project_root, state_path)
    inodes = {
        path.relative_to(project_root): path.stat().st_ino
        for path in project_root.rglob("*")
        if path.is_dir()
    }
    second = _run_bootstrap(project_root, state_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert inodes == {
        path.relative_to(project_root): path.stat().st_ino
        for path in project_root.rglob("*")
        if path.is_dir()
    }
    assert set(inodes) == {
        Path("backups"),
        Path("data"),
        Path("data/qdrant"),
        Path("data/state"),
        Path("logs"),
        Path("releases"),
        Path("shared"),
        Path("shared/corpora"),
        Path("shared/env"),
        Path("shared/env/candidates"),
    }
    root_owned = tuple(
        project_root / relative
        for relative in (
            "releases",
            "shared/corpora",
            "shared/env/candidates",
            "data",
            "data/qdrant",
            "backups",
        )
    )
    service_owned = (
        project_root / "data/state",
        project_root / "logs",
    )
    assert _fake_stat(state_path, root_owned) == ["0:0:700"] * 6
    assert _fake_stat(state_path, service_owned) == [
        "10001:10001:700",
        "10001:10001:700",
    ]


@pytest.mark.parametrize("unsafe", ("mode", "owner"))
def test_bootstrap_rejects_unsafe_existing_directory(
    tmp_path: Path,
    unsafe: str,
) -> None:
    """证明既有危险 owner/mode 不会被静默修正。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    state_path = tmp_path / "fakeroot.state"
    created = _run_bootstrap(project_root, state_path)
    assert created.returncode == 0, created.stderr
    releases = project_root / "releases"
    if unsafe == "mode":
        subprocess.run(  # noqa: S603
            [
                _FAKEROOT,
                "-i",
                str(state_path),
                "-s",
                str(state_path),
                "--",
                "/usr/bin/chmod",
                "0755",
                str(releases),
            ],
            check=True,
        )
        expected = "0:0:755"
    else:
        subprocess.run(  # noqa: S603
            [
                _FAKEROOT,
                "-i",
                str(state_path),
                "-s",
                str(state_path),
                "--",
                "/usr/bin/chown",
                "20000:20000",
                str(releases),
            ],
            check=True,
        )
        expected = "20000:20000:700"

    rejected = _run_bootstrap(project_root, state_path)

    assert rejected.returncode != 0
    assert "owner/mode" in rejected.stderr
    assert _fake_stat(state_path, (releases,)) == [expected]


def test_bootstrap_rejects_symlink_without_partial_creation(
    tmp_path: Path,
) -> None:
    """证明目标为符号链接时在创建其他目录前失败。"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (project_root / "releases").symlink_to(external, target_is_directory=True)

    completed = _run_bootstrap(project_root, tmp_path / "fakeroot.state")

    assert completed.returncode != 0
    assert "symbolic link" in completed.stderr
    assert {path.name for path in project_root.iterdir()} == {"releases"}
