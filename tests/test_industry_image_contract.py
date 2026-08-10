from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_DEFAULT_IMAGE = "docx-rag:d5c03cf9b97e"
_DOCKER = "/usr/bin/docker"


def _image() -> str:
    return os.environ.get("RAG_TEST_APP_IMAGE", _DEFAULT_IMAGE)


def test_updater_uses_the_image_entrypoint_for_asset_selfcheck() -> None:
    source = (
        _ROOT / "deployment" / "industry" / "update-app.sh"
    ).read_text(encoding="utf-8")

    assert "rag-app asset-selfcheck" not in source
    assert 'asset_report="$(docker run --rm --network none' in source
    assert '  asset-selfcheck)"' in source


def test_real_app_image_entrypoint_and_selfcheck_command() -> None:
    image = _image()
    inspection = subprocess.run(  # noqa: S603
        [
            _DOCKER,
            "image",
            "inspect",
            "--format",
            "{{json .Config.Entrypoint}}",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(inspection.stdout) == ["rag-app"]

    valid = subprocess.run(  # noqa: S603
        [
            _DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "asset-selfcheck",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert valid.returncode == 0, valid.stderr
    report = json.loads(valid.stdout)
    assert isinstance(report, dict)

    duplicated_entrypoint = subprocess.run(  # noqa: S603
        [
            _DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "rag-app",
            "asset-selfcheck",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert duplicated_entrypoint.returncode != 0
