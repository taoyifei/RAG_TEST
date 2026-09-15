"""检查湾事通分支是否保留固定 Universal Product Runtime。"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_GIT_EXECUTABLE = shutil.which("git")
_UNIVERSAL_BASE_SHA = "4f71f7db03990cf49cde0664bec68d6c25dfd544"
_INDUSTRY_REFERENCE_SHA = "5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a"
_UNIVERSAL_REMOTE_REF = "refs/remotes/origin/feature/universal-rag"
_WANSHITONG_REMOTE_REF = "refs/remotes/origin/feature/wanshitong"

_PROTECTED_CORE_ROOTS = (
    "src/rag_app/composition/product_runtime.py",
    "src/rag_app/product/provider_runtime.py",
    "src/rag_app/application/retrieval",
    "src/rag_app/product/grounded_runtime.py",
    "src/rag_app/api/p09.py",
    "src/rag_app/api/p09_stream.py",
    "src/rag_app/sdk.py",
    "src/rag_app/product/query_history.py",
    "src/rag_app/product/trace_coordinator.py",
    "src/rag_app/adapters/providers/openai_compatible.py",
)
_CRITICAL_BASELINE_TEST_PATHS = (
    "tests/api/test_grounded_product.py",
    "tests/api/test_operational_trace_product.py",
    "tests/api/test_p09_api.py",
    "tests/api/test_p09_stream.py",
    "tests/api/test_query_history.py",
    "tests/application/retrieval/test_evidence_precision.py",
    "tests/application/test_grounded_claim_validation.py",
    "tests/application/test_provider_runtime_registry.py",
    "tests/composition/test_product_runtime.py",
    "tests/e2e/test_p07_retrieval.py",
    "tests/sdk/test_p09_sdk.py",
    "tests/test_chunk_source_spans.py",
)
_REQUIRED_WB00_PATHS = (
    "docs/wanshitong/universal-first.md",
    "docs/wanshitong/architecture.md",
    "docs/wanshitong/phase-plan.md",
    "docs/wanshitong/demo-boundaries.md",
    "docs/wanshitong/ocr-capability.md",
    "scripts/check_wanshitong_universal_first.py",
)
_FORBIDDEN_WANSHITONG_RUNTIME_NAMES = frozenset(
    {"query_service.py", "provider_runtime.py"}
)
_TEST_BYPASS_MARKERS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "pytest.skip(",
    "pytest.xfail(",
    "unittest.skip",
    "#noqa",
)


class _GateBlockedError(RuntimeError):
    """表示门禁无法读取所需的 Git 或文件状态。"""


def _run_git(
    arguments: Sequence[str],
    *,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    if _GIT_EXECUTABLE is None:
        raise _GateBlockedError("找不到 git 可执行文件。")
    # 可执行文件已解析为绝对路径，参数只由本模块中的固定合同构造。
    completed = subprocess.run(
        [_GIT_EXECUTABLE, "-C", str(_REPOSITORY_ROOT), *arguments],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode not in accepted_returncodes:
        detail = completed.stderr.strip() or completed.stdout.strip()
        command = " ".join(arguments)
        raise _GateBlockedError(
            f"Git 命令失败，returncode={completed.returncode}: "
            f"git {command}: {detail}"
        )
    return completed


def _git_lines(arguments: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        line
        for line in _run_git(arguments).stdout.splitlines()
        if line
    )


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    completed = _run_git(
        ("merge-base", "--is-ancestor", ancestor, descendant),
        accepted_returncodes=(0, 1),
    )
    return completed.returncode == 0


def _require_commit(revision: str, label: str) -> None:
    try:
        _run_git(("cat-file", "-e", f"{revision}^{{commit}}"))
    except _GateBlockedError as error:
        raise _GateBlockedError(
            f"缺少 {label} commit {revision}；请先执行 git fetch。"
        ) from error


def _baseline_core_paths() -> tuple[str, ...]:
    paths = _git_lines(
        (
            "ls-tree",
            "-r",
            "--name-only",
            _UNIVERSAL_BASE_SHA,
            "--",
            *_PROTECTED_CORE_ROOTS,
        )
    )
    for root in _PROTECTED_CORE_ROOTS:
        prefix = f"{root.rstrip('/')}/"
        if not any(path == root or path.startswith(prefix) for path in paths):
            raise _GateBlockedError(f"固定基线中缺少受保护路径：{root}")
    return paths


def _missing_current_paths(paths: Sequence[str]) -> tuple[str, ...]:
    tracked = set(_git_lines(("ls-files", "--cached", "--", *paths)))
    return tuple(
        path
        for path in paths
        if path not in tracked or not (_REPOSITORY_ROOT / path).is_file()
    )


def _duplicate_wanshitong_runtimes() -> tuple[str, ...]:
    root = _REPOSITORY_ROOT / "src" / "rag_app" / "wanshitong"
    if not root.exists():
        return ()
    duplicates = (
        path.relative_to(_REPOSITORY_ROOT).as_posix()
        for path in root.rglob("*.py")
        if path.is_file()
        and path.name.casefold() in _FORBIDDEN_WANSHITONG_RUNTIME_NAMES
    )
    return tuple(sorted(duplicates))


def _added_test_bypasses() -> tuple[str, ...]:
    diff = _run_git(
        ("diff", "--unified=0", _UNIVERSAL_BASE_SHA, "--", "tests")
    ).stdout
    findings: list[str] = []
    for line_number, line in enumerate(diff.splitlines(), start=1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        compact = "".join(line[1:].casefold().split())
        if any(marker in compact for marker in _TEST_BYPASS_MARKERS):
            findings.append(f"diff-line-{line_number}:{line[1:].strip()}")
    return tuple(findings)


def _evaluate_gate() -> tuple[tuple[str, ...], int, int]:
    _require_commit(_UNIVERSAL_BASE_SHA, "Universal base")
    _require_commit(_INDUSTRY_REFERENCE_SHA, "Industry reference")

    findings: list[str] = []
    observed_base = _run_git(("rev-parse", _UNIVERSAL_REMOTE_REF)).stdout.strip()
    if observed_base != _UNIVERSAL_BASE_SHA:
        findings.append(
            "origin/feature/universal-rag 漂移："
            f"expected={_UNIVERSAL_BASE_SHA} actual={observed_base}"
        )

    base_is_ancestor = _is_ancestor(_UNIVERSAL_BASE_SHA, "HEAD")
    if not base_is_ancestor:
        findings.append("HEAD 不以固定 Universal SHA 为祖先。")
    if not _is_ancestor(_UNIVERSAL_BASE_SHA, _WANSHITONG_REMOTE_REF):
        findings.append("origin/feature/wanshitong 不以固定 Universal SHA 为祖先。")

    if base_is_ancestor:
        stage_commits = set(
            _git_lines(("rev-list", "HEAD", f"^{_UNIVERSAL_BASE_SHA}"))
        )
        industry_only = set(
            _git_lines(
                (
                    "rev-list",
                    _INDUSTRY_REFERENCE_SHA,
                    f"^{_UNIVERSAL_BASE_SHA}",
                )
            )
        )
        overlap = tuple(sorted(stage_commits & industry_only))
        if overlap:
            findings.append(
                "阶段祖先包含 Industry 独有提交：" + ",".join(overlap[:5])
            )
        equivalent = tuple(
            line[2:]
            for line in _git_lines(
                (
                    "cherry",
                    _INDUSTRY_REFERENCE_SHA,
                    "HEAD",
                    _UNIVERSAL_BASE_SHA,
                )
            )
            if line.startswith("- ")
        )
        if equivalent:
            findings.append(
                "阶段包含与 Industry 等价的 cherry-pick："
                + ",".join(equivalent[:5])
            )

    protected_paths = _baseline_core_paths()
    missing_core = _missing_current_paths(protected_paths)
    if missing_core:
        findings.append("受保护 Core 路径缺失：" + ",".join(missing_core))

    duplicates = _duplicate_wanshitong_runtimes()
    if duplicates:
        findings.append("湾事通目录出现第二套 Runtime：" + ",".join(duplicates))

    baseline_test_tree = set(
        _git_lines(
            (
                "ls-tree",
                "-r",
                "--name-only",
                _UNIVERSAL_BASE_SHA,
                "--",
                "tests",
            )
        )
    )
    unknown_tests = tuple(
        sorted(set(_CRITICAL_BASELINE_TEST_PATHS) - baseline_test_tree)
    )
    if unknown_tests:
        raise _GateBlockedError(
            "检查器声明了不在固定基线中的测试：" + ",".join(unknown_tests)
        )
    baseline_tests = tuple(
        sorted(
            path
            for path in baseline_test_tree
            if path.endswith(".py")
            and Path(path).name.startswith("test_")
        )
    )
    missing_tests = _missing_current_paths(baseline_tests)
    if missing_tests:
        findings.append("Universal 基线测试缺失：" + ",".join(missing_tests))

    bypasses = _added_test_bypasses()
    if bypasses:
        findings.append("测试差异新增绕过标记：" + ",".join(bypasses))

    missing_wb00 = _missing_current_paths(_REQUIRED_WB00_PATHS)
    if missing_wb00:
        findings.append("WB-00 必需文件未被跟踪或缺失：" + ",".join(missing_wb00))

    return tuple(findings), len(protected_paths), len(baseline_tests)


def main() -> int:
    """运行 Universal Preservation Gate。

    Args:
        无参数；使用固定 SHA 与当前 Git 工作树。

    Returns:
        通过返回 0，合同违规返回 1，无法检查返回 2。
    """
    try:
        findings, protected_count, baseline_test_count = _evaluate_gate()
    except _GateBlockedError as error:
        print(f"BLOCKED {error}")
        print("UNIVERSAL_PRESERVATION_GATE=BLOCKED")
        return 2

    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        print("UNIVERSAL_PRESERVATION_GATE=FAIL")
        return 1

    print(f"PASS universal_base={_UNIVERSAL_BASE_SHA}")
    print("PASS industry_ancestry_commits=0")
    print(f"PASS protected_core_files={protected_count}")
    print("PASS duplicate_wanshitong_runtimes=0")
    print(f"PASS baseline_tests={baseline_test_count}")
    print("PASS added_test_bypasses=0")
    print(f"PASS wb00_required_files={len(_REQUIRED_WB00_PATHS)}")
    print("UNIVERSAL_PRESERVATION_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
