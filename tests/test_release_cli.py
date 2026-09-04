"""P11 统一发布命令的纯离线回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release import _write_license_inventory, main


def test_license_inventory_is_sorted_and_does_not_invent_license(
    tmp_path: Path,
) -> None:
    sbom = tmp_path / "sbom.json"
    output = tmp_path / "licenses.json"
    sbom.write_text(
        json.dumps(
            {
                "components": [
                    {"name": "z", "version": "1"},
                    {
                        "name": "a",
                        "version": "2",
                        "purl": "pkg:pypi/a@2",
                        "licenses": [{"license": {"id": "MIT"}}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    _write_license_inventory(sbom, output)

    inventory = json.loads(output.read_text(encoding="utf-8"))["components"]
    assert [item["name"] for item in inventory] == ["a", "z"]
    assert inventory[0]["licenses"] == ["MIT"]
    assert inventory[1]["licenses"] == []


def test_release_cli_rejects_unknown_command_without_running_work() -> None:
    with pytest.raises(SystemExit):
        main(["not-a-command"])
