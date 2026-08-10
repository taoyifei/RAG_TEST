from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_OLD_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"


def _old_file(path: str) -> str:
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/git",
            "-C",
            str(_ROOT),
            "show",
            f"{_OLD_REVISION}:{path}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_old_revision_really_lacks_runtime_state_and_serving_env() -> None:
    """固定真实旧提交的 CLI、Compose 和 serving 配置语义。"""
    old_cli = _old_file("src/rag_app/cli.py")
    old_compose = _old_file("deployment/industry/compose.yaml")
    old_pipeline = _old_file("deployment/config/pipeline.json")
    current_pipeline = (_ROOT / "deployment/config/pipeline.json").read_text(
        encoding="utf-8"
    )

    assert "runtime-state" not in old_cli
    assert "RAG_UI_QUERY_AUTH_MODE" not in old_compose
    assert "RAG_TRACE_QUESTION_CAPTURE" not in old_compose
    assert old_pipeline != current_pipeline


def test_updater_does_not_require_old_app_runtime_state() -> None:
    """旧容器没有新 CLI 时，更新前快照必须由包内 helper 完成。"""
    script = (_ROOT / "deployment/industry/update-app.sh").read_text(
        encoding="utf-8"
    )

    assert "docker exec rag-industry-app rag-app runtime-state" not in script
    assert "runtime_check.py pre-update-index-state" in script


def test_updater_uses_bundled_verify_instead_of_old_release_verify() -> None:
    """更新终验必须来自当前包内版本化 runtime。"""
    script = (_ROOT / "deployment/industry/update-app.sh").read_text(
        encoding="utf-8"
    )

    assert "verify-app-update.sh" in script
    assert 'dirname "${compose_file}"' not in script
