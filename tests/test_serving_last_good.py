from __future__ import annotations

import json
from pathlib import Path

import pytest

from deployment.industry.serving_last_good import (
    LastGoodError,
    migrate_legacy_last_good,
    promote_last_good,
    resolve_last_good,
)

_FIRST_REVISION = "1" * 40
_SECOND_REVISION = "2" * 40


def _private_inputs(
    tmp_path: Path,
    revision: str,
    *,
    stem: str,
) -> tuple[Path, Path]:
    env = tmp_path / f"{stem}.env"
    state = tmp_path / f"{stem}.json"
    env.write_text(
        f"RAG_RELEASE_REVISION={revision}\nRAG_APP_IMAGE=docx-rag:{revision[:12]}\n",
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "revision": revision,
                "schema_version": "2",
                "stage": "last_good",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    state.chmod(0o600)
    return env, state


@pytest.mark.parametrize(
    "failure_point",
    ("after_env", "after_state", "after_manifest", "before_pointer"),
)
def test_interrupted_promotion_keeps_old_pointer_authoritative(
    tmp_path: Path,
    failure_point: str,
) -> None:
    backup = tmp_path / "backups"
    first_env, first_state = _private_inputs(
        tmp_path, _FIRST_REVISION, stem="first"
    )
    second_env, second_state = _private_inputs(
        tmp_path, _SECOND_REVISION, stem="second"
    )
    first_pointer = promote_last_good(
        backup, first_env, first_state, _FIRST_REVISION
    )

    with pytest.raises(LastGoodError, match="INJECTED"):
        promote_last_good(
            backup,
            second_env,
            second_state,
            _SECOND_REVISION,
            failure_point=failure_point,  # type: ignore[arg-type]
        )

    resolved = resolve_last_good(backup)
    current_pointer = json.loads(
        (backup / "last-good-pointer.json").read_bytes()
    )
    assert current_pointer == first_pointer
    assert resolved["revision"] == _FIRST_REVISION
    assert resolved["snapshot_id"] == first_pointer["snapshot_id"]


def test_corrupt_pointer_and_snapshot_sha_fail_closed(tmp_path: Path) -> None:
    backup = tmp_path / "backups"
    env, state = _private_inputs(tmp_path, _FIRST_REVISION, stem="first")
    promote_last_good(backup, env, state, _FIRST_REVISION)
    pointer_path = backup / "last-good-pointer.json"
    valid_pointer = pointer_path.read_bytes()

    pointer_path.write_text("{}\n", encoding="utf-8")
    pointer_path.chmod(0o600)
    with pytest.raises(LastGoodError, match="POINTER_FIELDS"):
        resolve_last_good(backup)

    pointer_path.write_bytes(valid_pointer)
    pointer_path.chmod(0o600)
    resolved = resolve_last_good(backup)
    snapshot_env = Path(str(resolved["env_path"]))
    snapshot_env.write_text("tampered\n", encoding="utf-8")
    snapshot_env.chmod(0o600)
    with pytest.raises(LastGoodError, match="FILE_IDENTITY"):
        resolve_last_good(backup)


def test_legacy_pair_is_migrated_once_then_ceases_to_be_authoritative(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    env, state = _private_inputs(tmp_path, _FIRST_REVISION, stem="legacy")
    legacy_env = backup / "last-good.env"
    legacy_state = backup / "last-good.json"
    legacy_env.write_bytes(env.read_bytes())
    legacy_state.write_bytes(state.read_bytes())
    legacy_env.chmod(0o600)
    legacy_state.chmod(0o600)

    first = migrate_legacy_last_good(backup)
    legacy_env.write_text(
        f"RAG_RELEASE_REVISION={_SECOND_REVISION}\n",
        encoding="utf-8",
    )
    legacy_env.chmod(0o600)
    second = migrate_legacy_last_good(backup)

    assert first == second
    assert second["revision"] == _FIRST_REVISION
