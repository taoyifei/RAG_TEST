"""最终部署事务的静态安全边界测试。"""

from pathlib import Path

_ROOT = Path(__file__).parents[1]


def _script(name: str) -> str:
    return (_ROOT / "deployment" / name).read_text(encoding="utf-8")


def test_deploy_separates_candidate_active_and_late_rollback_state() -> None:
    deploy = _script("deploy.sh")

    assert 'active_env="${shared_env_dir}/rag.env"' in deploy
    assert 'candidate_dir="${shared_env_dir}/candidates"' in deploy
    assert "publish_rollback_state" in deploy
    assert deploy.index("wait_for_runtime_health") < deploy.index(
        "publish_rollback_state"
    )
    assert deploy.index("commit_candidate_env") < deploy.index(
        "publish_rollback_state"
    )


def test_deploy_and_rollback_share_complete_health_gate() -> None:
    for name in ("deploy.sh", "rollback.sh"):
        script = _script(name)
        assert "wait_for_runtime_health" in script
        for container in ("rag-qdrant", "rag-ocr", "rag-app"):
            assert f'wait_for_container_health "{container}"' in script
        assert "/live" in script
        assert "/ready" not in script


def test_rollback_records_and_restores_degraded_runtime() -> None:
    rollback = _script("rollback.sh")

    assert "capture_container_state" in rollback
    assert "restore_original_runtime" in rollback
    assert "回滚调用前的核心容器必须全部运行" not in rollback
    assert "ORIGINAL_APP_EXISTS" in rollback
    assert "ORIGINAL_OCR_EXISTS" in rollback
    assert "ORIGINAL_QDRANT_EXISTS" in rollback
