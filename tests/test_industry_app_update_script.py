from pathlib import Path

_ROOT = Path(__file__).parents[1]


def _script(name: str) -> str:
    return (_ROOT / "deployment/industry" / name).read_text(encoding="utf-8")


def test_updater_uses_private_env_and_bundled_runtime() -> None:
    updater = _script("update-app.sh")

    assert '[[ "$#" -eq 1 ]]' in updater
    assert '"${package_dir}/package_selfcheck.py"' in updater
    assert 'source "${runtime_dir}/lib.sh"' in updater
    assert 'bash "${runtime_dir}/verify-app-update.sh"' in updater
    assert 'bash "${runtime_dir}/rollback-app-update.sh"' in updater
    assert (
        "rag-app runtime-state"
        not in updater.split("run_industry_compose", maxsplit=1)[0]
    )
    assert "/verify.sh" not in updater
    assert "run-index.sh" not in updater


def test_updater_parses_json_with_python_and_never_sources_private_env() -> (
    None
):
    updater = _script("update-app.sh")

    assert "json.loads" in updater
    assert 'source "${env_file}"' not in updater
    assert "source ${env_file}" not in updater
    assert "sed " not in updater
    assert "grep " not in updater
    assert "awk " not in updater


def test_update_and_rollback_force_recreate_only_industry_app() -> None:
    updater = _script("update-app.sh")
    rollback = _script("rollback-app-update.sh")
    command = (
        "up -d --no-deps --no-build --pull never --force-recreate "
        "rag-industry-app"
    )

    normalized_updater = " ".join(updater.replace("\\\n", " ").split())
    normalized_rollback = " ".join(rollback.replace("\\\n", " ").split())
    assert command in normalized_updater
    assert command in normalized_rollback
    for script in (updater, rollback):
        assert "--force-recreate rag-industry-worker" not in script
        assert "--force-recreate rag-industry-ocr" not in script
        assert "--force-recreate rag-industry-qdrant" not in script
        assert " index full" not in script
        assert " index incremental" not in script
