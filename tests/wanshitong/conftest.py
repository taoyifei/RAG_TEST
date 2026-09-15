"""湾事通公共 Facade 的共享 pytest fixture。"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from tests.wanshitong.support import PublicHarness, build_public_harness


@pytest.fixture
def public_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[PublicHarness, None, None]:
    harness = build_public_harness(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        harness.close()
