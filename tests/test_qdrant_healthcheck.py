from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

_QDRANT_IMAGE = (
    "qdrant/qdrant:v1.18.3@sha256:"
    "0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
)


def _qdrant_service() -> dict[str, object]:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load(
        (root / "deployment/compose.yaml").read_text(encoding="utf-8")
    )
    return compose["services"]["rag-qdrant"]


def test_qdrant_healthcheck_uses_fixed_image_posix_command() -> None:
    service = _qdrant_service()
    healthcheck = service["healthcheck"]
    command = healthcheck["test"]

    assert _QDRANT_IMAGE in service["image"]
    assert command[0] == "CMD"
    assert command[1] == "/usr/bin/grep"
    assert command[2] == "-Eq"
    assert "/dev/tcp" not in "\n".join(command)
    assert command[-2:] == ["/proc/net/tcp", "/proc/net/tcp6"]


def test_qdrant_healthcheck_rejects_wrong_listening_port(
    tmp_path: Path,
) -> None:
    service = _qdrant_service()
    command = service["healthcheck"]["test"]
    probe = command[1:4]
    expected = tmp_path / "expected"
    wrong = tmp_path / "wrong"
    expected.write_text(
        "  0: 0100007F:18BD 00000000:0000 0A 00000000\n",
        encoding="ascii",
    )
    wrong.write_text(
        "  0: 0100007F:FFFE 00000000:0000 0A 00000000\n",
        encoding="ascii",
    )

    expected_result = subprocess.run(  # noqa: S603
        [*probe, expected],
        check=False,
        capture_output=True,
        text=True,
    )
    wrong_result = subprocess.run(  # noqa: S603
        [*probe, wrong],
        check=False,
        capture_output=True,
        text=True,
    )

    assert expected_result.returncode == 0
    assert wrong_result.returncode != 0
