from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_LIB = _ROOT / "deployment/industry/lib.sh"
_REVISION = "a" * 40
_IMAGE = f"docx-rag:{_REVISION[:12]}"
_INDEX = {
    "active_collection": "rag-docx-active-1",
    "alias": "rag-industry-active",
    "index_fingerprint": "sha256:" + "1" * 64,
    "manifest_sha256": "2" * 64,
    "point_count": 139,
}


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _release(
    tmp_path: Path,
    *,
    ocr_mode: str = "dedicated",
) -> tuple[Path, Path, Path]:
    release = tmp_path / "release"
    release.mkdir()
    compose = release / "compose.yaml"
    compose.write_text("name: rag-industry\nservices: {}\n", encoding="utf-8")
    manifest = {
        "corpus": {"sha256": "3" * 64},
        "images": {
            "ocr": {
                "id": "sha256:" + "4" * 64,
                "ref": "docx-rag-ocr:fixed",
                "revision": "5" * 40,
            }
        },
    }
    (release / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    backup = tmp_path / "backups"
    env_file = tmp_path / "rag-industry.env"
    env_file.write_text(
        "\n".join(
            (
                f"RAG_APP_IMAGE={_IMAGE}",
                f"RAG_RELEASE_REVISION={_REVISION}",
                f"RAG_INDUSTRY_COMPOSE_FILE={compose}",
                f"RAG_BACKUP_PATH={backup}",
                f"RAG_DOCS_PATH={tmp_path / 'docs'}",
                f"RAG_CONFIG_PATH={tmp_path / 'config'}",
                f"RAG_STATE_PATH={tmp_path / 'state'}",
                "RAG_QDRANT_ALIAS=rag-industry-active",
                "RAG_PORT=8188",
                f"RAG_OCR_MODE={ocr_mode}",
                "RAG_INDUSTRY_OCR_GPU_DEVICE_ID=3",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return release, env_file, backup


def _bash(
    command: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/usr/bin/bash", "-c", command, "test", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(environment or {})},
        timeout=30,
    )


def test_release_state_promotes_only_after_verified(tmp_path: Path) -> None:
    _, env_file, backup = _release(tmp_path)
    command = (
        'source "$1"; '
        'write_industry_release_state "$2" candidate; '
        'write_industry_release_state "$2" deployed'
    )

    deployed = _bash(command, str(_LIB), str(env_file))

    assert deployed.returncode == 0, deployed.stderr
    assert json.loads((backup / "deployment-state.json").read_text())[
        "stage"
    ] == "deployed"
    assert not (backup / "last-good.env").exists()
    assert not (backup / "last-good.json").exists()

    index_json = json.dumps(_INDEX, separators=(",", ":"), sort_keys=True)
    indexed = _bash(
        'source "$1"; write_industry_release_state "$2" indexed "$3"',
        str(_LIB),
        str(env_file),
        index_json,
    )

    assert indexed.returncode == 0, indexed.stderr
    assert json.loads((backup / "deployment-state.json").read_text())[
        "stage"
    ] == "indexed"
    assert not (backup / "last-good.env").exists()

    promoted = _bash(
        'source "$1"; promote_industry_last_good "$2" "$3"',
        str(_LIB),
        str(env_file),
        index_json,
    )

    assert promoted.returncode == 0, promoted.stderr
    assert (backup / "last-good.env").read_text() == env_file.read_text()
    last_good = json.loads((backup / "last-good.json").read_text())
    assert last_good["stage"] == "verified"
    assert last_good["index"] == _INDEX
    assert json.loads((backup / "deployment-state.json").read_text())[
        "stage"
    ] == "last_good"
    assert not list(backup.glob(".*.??????"))


def test_candidate_cannot_skip_indexed_state(tmp_path: Path) -> None:
    _, env_file, backup = _release(tmp_path)
    result = _bash(
        'source "$1"; '
        'write_industry_release_state "$2" candidate; '
        'write_industry_release_state "$2" verified "$3"',
        str(_LIB),
        str(env_file),
        json.dumps(_INDEX),
    )

    assert result.returncode != 0
    assert json.loads((backup / "deployment-state.json").read_text())[
        "stage"
    ] == "candidate"
    assert not (backup / "last-good.env").exists()


def test_app_candidate_promotes_without_reindex(tmp_path: Path) -> None:
    _, env_file, backup = _release(tmp_path)
    backup.mkdir()
    candidate = {
        "base": {"image": "docx-rag:old", "revision": "b" * 40},
        "index": _INDEX,
        "schema_version": "1",
        "stage": "candidate",
        "target": {"image": _IMAGE, "revision": _REVISION},
        "update_kind": "app_only",
    }
    (backup / "app-candidate.json").write_text(
        json.dumps(candidate),
        encoding="utf-8",
    )

    result = _bash(
        'source "$1"; promote_industry_last_good "$2" "$3"',
        str(_LIB),
        str(env_file),
        json.dumps(_INDEX, separators=(",", ":"), sort_keys=True),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((backup / "app-candidate.json").read_text())[
        "stage"
    ] == "last_good"
    assert json.loads((backup / "last-good.json").read_text())[
        "stage"
    ] == "verified"
    assert not (backup / "deployment-state.json").exists()


def _write_ocr_fakes(
    binary_dir: Path,
    *,
    scenario: str,
) -> None:
    active = "" if scenario == "idle" else "123"
    _write_executable(
        binary_dir / "nvidia-smi",
        f"""#!/usr/bin/env bash
if [[ "$*" == *'query-compute-apps=pid'* ]]; then
  printf '%s\n' {active!r}
else
  printf '3, 10240 MiB\n'
fi
""",
    )
    project = "rag-simple" if scenario == "training" else "rag-industry"
    image_id = (
        "sha256:" + "9" * 64
        if scenario == "mismatch"
        else "sha256:" + "4" * 64
    )
    missing = "true" if scenario == "unknown" else "false"
    _write_executable(
        binary_dir / "docker",
        f"""#!/usr/bin/env bash
if [[ "$1 $2 $3" == 'container inspect rag-industry-ocr' \
  && "$*" != *'--format'* && {missing} == true ]]; then
  exit 1
fi
if [[ "$1 $2" == 'container inspect' ]]; then
  template="$4"
  case "$template" in
    *compose.project*) echo {project!r} ;;
    *compose.service*) echo rag-industry-ocr ;;
    '{{{{.Config.Image}}}}') echo docx-rag-ocr:fixed ;;
    '{{{{.Image}}}}') echo {image_id!r} ;;
    *State.Health*) echo healthy ;;
    *DeviceRequests*) echo '[{{"DeviceIDs":["3"]}}]' ;;
  esac
  exit 0
fi
if [[ "$1 $2" == 'image inspect' ]]; then
  echo {'5' * 40!r}
  exit 0
fi
if [[ "$1" == top ]]; then
  printf 'PID\n123\n'
  exit 0
fi
exit 2
""",
    )


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    (
        ("idle", None),
        ("managed", None),
        ("unknown", "OCR_GPU_UNKNOWN_PID"),
        ("training", "OCR_GPU_OWNED_BY_OTHER_PROJECT"),
        ("mismatch", "OCR_GPU_MANAGED_OCR_IDENTITY_MISMATCH"),
    ),
)
def test_dedicated_ocr_gpu_ownership_matrix(
    tmp_path: Path,
    scenario: str,
    expected_code: str | None,
) -> None:
    release, env_file, _ = _release(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _write_ocr_fakes(binary_dir, scenario=scenario)

    result = _bash(
        'source "$1"; validate_industry_ocr_gpu_ownership "$2" "$3"',
        str(_LIB),
        str(env_file),
        str(release),
        environment={"PATH": f"{binary_dir}:/usr/bin:/bin"},
    )

    if expected_code is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert expected_code in result.stderr


def test_external_ocr_does_not_probe_local_gpu(tmp_path: Path) -> None:
    release, env_file, _ = _release(tmp_path, ocr_mode="external")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _write_executable(
        binary_dir / "nvidia-smi",
        "#!/usr/bin/env bash\nexit 99\n",
    )

    result = _bash(
        'source "$1"; validate_industry_ocr_gpu_ownership "$2" "$3"',
        str(_LIB),
        str(env_file),
        str(release),
        environment={"PATH": f"{binary_dir}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr


def test_industry_compose_ignores_polluted_rag_environment(
    tmp_path: Path,
) -> None:
    release, env_file, _ = _release(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    log = tmp_path / "docker.log"
    compose_payload = {
        "name": "rag-industry",
        "services": {
            "rag-industry-app": {
                "environment": {"RAG_QDRANT_ALIAS": "rag-industry-active"},
                "image": _IMAGE,
                "ports": [{"published": "8188", "target": 8088}],
                "volumes": [
                    {"source": str(tmp_path / "config"), "target": "/config"},
                    {"source": str(tmp_path / "docs"), "target": "/data/docs"},
                    {"source": str(tmp_path / "state"), "target": "/state"},
                ],
            }
        },
    }
    _write_executable(
        binary_dir / "docker",
        f"""#!/usr/bin/env bash
printf 'port=%s image=%s alias=%s args=%s\n' \
  "${{RAG_PORT-unset}}" "${{RAG_APP_IMAGE-unset}}" \
  "${{RAG_QDRANT_ALIAS-unset}}" "$*" >> {str(log)!r}
printf '%s\n' {json.dumps(compose_payload)!r}
""",
    )

    result = _bash(
        'source "$1"; validate_industry_compose "$2" "$3"',
        str(_LIB),
        str(env_file),
        str(release / "compose.yaml"),
        environment={
            "PATH": f"{binary_dir}:/usr/bin:/bin",
            "RAG_APP_IMAGE": "polluted:image",
            "RAG_PORT": "9999",
            "RAG_QDRANT_ALIAS": "polluted-alias",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "port=unset image=unset alias=unset" in log.read_text()
