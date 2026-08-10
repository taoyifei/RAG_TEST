"""将工业旧版 DOC 转换为安全、可审计的 DOCX corpus。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.industry_corpus import (  # noqa: E402
    EXPECTED_INVENTORY,
    CorpusPreparationError,
    prepare_industry_corpus,
)
from scripts.industry_corpus.workflow import (  # noqa: E402
    load_expected_inventory,
)


def _arguments() -> argparse.Namespace:
    """解析工业 corpus 预处理参数。

    Returns:
        已解析参数。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--libreoffice-path", type=Path, required=True)
    parser.add_argument(
        "--manifest-output",
        default="industry-corpus-manifest.json",
    )
    parser.add_argument(
        "--audit-output",
        default="industry-corpus-audit.json",
    )
    parser.add_argument("--expected-inventory", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    return parser.parse_args()


def _git_head(repository_root: Path) -> str:
    """读取当前仓库完整 HEAD。

    Args:
        repository_root: Git 根目录。

    Returns:
        完整小写 Git SHA。

    Raises:
        CorpusPreparationError: Git 不可用或 HEAD 无效。

    """
    git = shutil.which("git")
    if git is None:
        raise CorpusPreparationError("GIT_NOT_FOUND")
    try:
        completed = subprocess.run(  # noqa: S603
            [git, "-C", str(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CorpusPreparationError("GIT_HEAD_FAILED") from error
    return completed.stdout.strip()


def main() -> int:
    """执行完整工业 corpus 预处理。

    Args:
        无参数；命令行选项由当前进程解析。

    Returns:
        成功返回 0，安全门禁失败返回 1。

    """
    arguments = _arguments()
    try:
        expected = (
            EXPECTED_INVENTORY
            if arguments.expected_inventory is None
            else load_expected_inventory(arguments.expected_inventory)
        )
        result = prepare_industry_corpus(
            source_dir=arguments.source_dir,
            output_root=arguments.output_root,
            libreoffice_path=arguments.libreoffice_path,
            source_date_epoch=arguments.source_date_epoch,
            generated_from_git_sha=_git_head(_REPOSITORY_ROOT),
            timeout_seconds=arguments.timeout_seconds,
            expected_inventory=expected,
            manifest_name=arguments.manifest_output,
            audit_name=arguments.audit_output,
        )
    except CorpusPreparationError as error:
        print(f"INDUSTRY_CORPUS_PREPARATION_FAILED: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "active_document_count": result.active_document_count,
                "corpus_revision": result.corpus_revision,
                "corpus_sha256": result.corpus_sha256,
                "reference_document_count": result.reference_document_count,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
