from pathlib import Path


def test_backup_manifest_includes_identity_metadata_without_env_content(
) -> None:
    backup = (
        Path(__file__).parents[1] / "deployment/backup.sh"
    ).read_text(encoding="utf-8")

    assert "BACKUP_METADATA.json" in backup
    assert "python3 - \\" in backup
    assert "active_env_sha256" in backup
    assert "PRAGMA query_only = ON" in backup
    assert "sha256sum state.tar.gz qdrant.tar.gz BACKUP_METADATA.json" in backup


def test_app_restore_uses_bounded_live_wait() -> None:
    backup = (
        Path(__file__).parents[1] / "deployment/backup.sh"
    ).read_text(encoding="utf-8")

    assert "wait_for_app_live" in backup
    assert "max_attempts=30" in backup
    assert "--connect-timeout 2 --max-time 5" in backup


def test_backup_restore_uses_authenticated_qdrant_ready_probe() -> None:
    backup = (
        Path(__file__).parents[1] / "deployment/backup.sh"
    ).read_text(encoding="utf-8")

    assert "wait_for_qdrant_ready" in backup
    assert "docker exec rag-app python -c" in backup
    assert 'os.environ["RAG_QDRANT_API_KEY"]' in backup
    assert 'f"{base_url}/readyz"' in backup
