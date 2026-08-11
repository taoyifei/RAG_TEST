from __future__ import annotations

import json
from pathlib import Path

import pytest

from deployment.industry.serving_last_good import (
    LastGoodError,
    checkpoint_source_last_good,
    finalize_target_last_good,
    inspect_last_good,
    migrate_legacy_last_good,
    promote_last_good,
    resolve_last_good,
)

_FIRST_REVISION = "1" * 40
_SECOND_REVISION = "2" * 40
_THIRD_REVISION = "3" * 40


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


def _write_private_json(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _update_manifest(
    tmp_path: Path,
    source_revision: str,
    *older_revisions: str,
) -> Path:
    return _write_private_json(
        tmp_path / "UPDATE_MANIFEST.json",
        {
            "source_compatibility": {
                "trusted_last_good_revisions": [
                    source_revision,
                    *older_revisions,
                ]
            }
        },
    )


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


@pytest.mark.parametrize("bad_layout", ("symlink", "public", "duplicate"))
def test_legacy_env_only_rejects_untrusted_file_or_duplicate_revision(
    tmp_path: Path,
    bad_layout: str,
) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    env, _ = _private_inputs(tmp_path, _FIRST_REVISION, stem="source")
    legacy = backup / "last-good.env"
    if bad_layout == "symlink":
        legacy.symlink_to(env)
    else:
        legacy.write_bytes(env.read_bytes())
        legacy.chmod(0o644 if bad_layout == "public" else 0o600)
        if bad_layout == "duplicate":
            legacy.write_text(
                env.read_text(encoding="utf-8")
                + f"RAG_RELEASE_REVISION={_FIRST_REVISION}\n",
                encoding="utf-8",
            )
            legacy.chmod(0o600)

    with pytest.raises(LastGoodError):
        inspect_last_good(backup)


def test_legacy_layouts_are_classified_without_rewriting_evidence(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    env, state = _private_inputs(tmp_path, _FIRST_REVISION, stem="source")
    legacy_env = backup / "last-good.env"
    legacy_state = backup / "last-good.json"
    legacy_env.write_bytes(env.read_bytes())
    legacy_env.chmod(0o600)
    env_only_bytes = legacy_env.read_bytes()

    env_only = inspect_last_good(backup)

    assert env_only["state"] == "legacy_env_only"
    assert legacy_env.read_bytes() == env_only_bytes
    legacy_state.write_bytes(state.read_bytes())
    legacy_state.chmod(0o600)
    pair_bytes = (legacy_env.read_bytes(), legacy_state.read_bytes())
    pair_inspection = inspect_last_good(backup)
    assert pair_inspection["state"] == "legacy_pair"
    inspection = _write_private_json(
        tmp_path / "pair-inspection.json", pair_inspection
    )
    checkpoint = checkpoint_source_last_good(
        backup,
        env,
        state,
        _FIRST_REVISION,
        inspection,
        _update_manifest(tmp_path, _FIRST_REVISION),
    )
    assert checkpoint["revision"] == _FIRST_REVISION
    assert checkpoint["source_snapshot"]["state_sha256"]  # type: ignore[index]
    assert (legacy_env.read_bytes(), legacy_state.read_bytes()) == pair_bytes
    legacy_env.unlink()
    with pytest.raises(LastGoodError, match="STATE_ONLY"):
        inspect_last_good(backup)


def test_env_only_checkpoint_requires_exact_current_source_env(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    env, state = _private_inputs(tmp_path, _FIRST_REVISION, stem="source")
    legacy = backup / "last-good.env"
    legacy.write_bytes(env.read_bytes() + b"RAG_PORT=9999\n")
    legacy.chmod(0o600)
    inspection = _write_private_json(
        tmp_path / "inspection.json", inspect_last_good(backup)
    )
    manifest = _update_manifest(tmp_path, _FIRST_REVISION)

    with pytest.raises(LastGoodError, match="SOURCE_MISMATCH"):
        checkpoint_source_last_good(
            backup,
            env,
            state,
            _FIRST_REVISION,
            inspection,
            manifest,
        )


def test_older_trusted_pointer_is_preserved_then_current_source_checkpointed(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    old_env, old_state = _private_inputs(
        tmp_path, _FIRST_REVISION, stem="old"
    )
    source_env, source_state = _private_inputs(
        tmp_path, _SECOND_REVISION, stem="source"
    )
    old_pointer = promote_last_good(
        backup, old_env, old_state, _FIRST_REVISION
    )
    inspection = _write_private_json(
        tmp_path / "inspection.json", inspect_last_good(backup)
    )
    manifest = _update_manifest(
        tmp_path, _SECOND_REVISION, _FIRST_REVISION
    )

    checkpoint = checkpoint_source_last_good(
        backup,
        source_env,
        source_state,
        _SECOND_REVISION,
        inspection,
        manifest,
    )

    assert resolve_last_good(backup)["revision"] == _SECOND_REVISION
    assert checkpoint["pointer_before"]["revision"] == _FIRST_REVISION  # type: ignore[index]
    assert checkpoint["pointer_after"]["revision"] == _SECOND_REVISION  # type: ignore[index]
    current_pointer = (backup / "last-good-pointer.json").read_bytes()
    _write_private_json(backup / "last-good-pointer.json", old_pointer)
    assert resolve_last_good(backup)["revision"] == _FIRST_REVISION
    (backup / "last-good-pointer.json").write_bytes(current_pointer)
    (backup / "last-good-pointer.json").chmod(0o600)
    assert resolve_last_good(backup)["revision"] == _SECOND_REVISION


def test_unknown_pointer_revision_cannot_be_replaced_by_source_checkpoint(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    unknown_env, unknown_state = _private_inputs(
        tmp_path, _THIRD_REVISION, stem="unknown"
    )
    source_env, source_state = _private_inputs(
        tmp_path, _SECOND_REVISION, stem="source"
    )
    promote_last_good(
        backup, unknown_env, unknown_state, _THIRD_REVISION
    )
    inspection = _write_private_json(
        tmp_path / "inspection.json", inspect_last_good(backup)
    )
    manifest = _update_manifest(
        tmp_path, _SECOND_REVISION, _FIRST_REVISION
    )

    with pytest.raises(LastGoodError, match="REVISION_UNTRUSTED"):
        checkpoint_source_last_good(
            backup,
            source_env,
            source_state,
            _SECOND_REVISION,
            inspection,
            manifest,
        )
    assert resolve_last_good(backup)["revision"] == _THIRD_REVISION


def test_absent_pointer_can_promote_target_and_target_reentry_is_idempotent(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    source_env, source_state = _private_inputs(
        tmp_path, _FIRST_REVISION, stem="source"
    )
    target_env, target_state = _private_inputs(
        tmp_path, _SECOND_REVISION, stem="target"
    )
    inspection = _write_private_json(
        tmp_path / "inspection.json", inspect_last_good(backup)
    )
    manifest = _update_manifest(tmp_path, _FIRST_REVISION)
    checkpoint_value = checkpoint_source_last_good(
        backup,
        source_env,
        source_state,
        _FIRST_REVISION,
        inspection,
        manifest,
    )
    checkpoint = _write_private_json(
        tmp_path / "source-checkpoint.json", checkpoint_value
    )
    assert not (backup / "last-good-pointer.json").exists()

    first = finalize_target_last_good(
        backup,
        target_env,
        target_state,
        _SECOND_REVISION,
        inspection,
        source_state,
        checkpoint,
    )
    pointer_bytes = (backup / "last-good-pointer.json").read_bytes()
    second = finalize_target_last_good(
        backup,
        target_env,
        target_state,
        _SECOND_REVISION,
        inspection,
        source_state,
        checkpoint,
    )

    assert first == second
    assert first["revision"] == _SECOND_REVISION
    assert (backup / "last-good-pointer.json").read_bytes() == pointer_bytes


def test_finalize_rejects_third_pointer_after_source_checkpoint(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    source_env, source_state = _private_inputs(
        tmp_path, _FIRST_REVISION, stem="source"
    )
    target_env, target_state = _private_inputs(
        tmp_path, _SECOND_REVISION, stem="target"
    )
    third_env, third_state = _private_inputs(
        tmp_path, _THIRD_REVISION, stem="third"
    )
    promote_last_good(
        backup, source_env, source_state, _FIRST_REVISION
    )
    inspection = _write_private_json(
        tmp_path / "inspection.json", inspect_last_good(backup)
    )
    checkpoint = _write_private_json(
        tmp_path / "source-checkpoint.json",
        checkpoint_source_last_good(
            backup,
            source_env,
            source_state,
            _FIRST_REVISION,
            inspection,
            _update_manifest(tmp_path, _FIRST_REVISION),
        ),
    )
    promote_last_good(backup, third_env, third_state, _THIRD_REVISION)

    with pytest.raises(LastGoodError, match="POINTER_STATE_MISMATCH"):
        finalize_target_last_good(
            backup,
            target_env,
            target_state,
            _SECOND_REVISION,
            inspection,
            source_state,
            checkpoint,
        )
    assert resolve_last_good(backup)["revision"] == _THIRD_REVISION


def test_finalize_rejects_corrupt_source_checkpoint_snapshot(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backups"
    backup.mkdir()
    source_env, source_state = _private_inputs(
        tmp_path, _FIRST_REVISION, stem="source"
    )
    target_env, target_state = _private_inputs(
        tmp_path, _SECOND_REVISION, stem="target"
    )
    inspection = _write_private_json(
        tmp_path / "inspection.json", inspect_last_good(backup)
    )
    checkpoint_value = checkpoint_source_last_good(
        backup,
        source_env,
        source_state,
        _FIRST_REVISION,
        inspection,
        _update_manifest(tmp_path, _FIRST_REVISION),
    )
    checkpoint = _write_private_json(
        tmp_path / "source-checkpoint.json", checkpoint_value
    )
    snapshot_id = checkpoint_value["source_snapshot"]["snapshot_id"]  # type: ignore[index]
    snapshot_env = (
        backup / "last-good-snapshots" / str(snapshot_id) / "rag-industry.env"
    )
    snapshot_env.write_text("tampered\n", encoding="utf-8")
    snapshot_env.chmod(0o600)

    with pytest.raises(LastGoodError, match="FILE_IDENTITY"):
        finalize_target_last_good(
            backup,
            target_env,
            target_state,
            _SECOND_REVISION,
            inspection,
            source_state,
            checkpoint,
        )
    assert not (backup / "last-good-pointer.json").exists()
