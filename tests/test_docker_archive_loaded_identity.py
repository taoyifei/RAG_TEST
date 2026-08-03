"""验证 Docker 加载后镜像在两种 store 下的可移植身份。"""

from __future__ import annotations

import pytest

from scripts.docker_archive_loaded_identity import (
    LoadedImageIdentityError,
    validate_loaded_image_identity,
)

_MANIFEST = "sha256:" + "1" * 64
_CONFIG = "sha256:" + "2" * 64
_REVISION = "3" * 40


def _inspect_payload(
    image_id: str,
    *,
    descriptor: object = None,
) -> list[dict[str, object]]:
    """构造 Docker image inspect 的最小真实结构。

    Args:
        image_id: Docker `.Id`。
        descriptor: 可选 Docker `.Descriptor`。

    Returns:
        可传给验证器的单镜像 inspect 列表。

    """
    payload: dict[str, object] = {
        "Architecture": "amd64",
        "Config": {
            "Labels": {"org.opencontainers.image.revision": _REVISION}
        },
        "Id": image_id,
        "Os": "linux",
    }
    if descriptor is not None:
        payload["Descriptor"] = descriptor
    return [payload]


@pytest.mark.parametrize("image_id", (_MANIFEST, _CONFIG))
def test_containerd_store_accepts_manifest_descriptor(
    image_id: str,
) -> None:
    mode = validate_loaded_image_identity(
        _inspect_payload(image_id, descriptor={"digest": _MANIFEST}),
        expected_manifest_digest=_MANIFEST,
        expected_config_digest=_CONFIG,
        expected_platform="linux/amd64",
        expected_revision=_REVISION,
    )

    assert mode == "containerd"


def test_classic_store_requires_config_id() -> None:
    mode = validate_loaded_image_identity(
        _inspect_payload(_CONFIG),
        expected_manifest_digest=_MANIFEST,
        expected_config_digest=_CONFIG,
        expected_platform="linux/amd64",
        expected_revision=_REVISION,
    )

    assert mode == "classic"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    (
        (_inspect_payload(_MANIFEST), "classic store"),
        (
            _inspect_payload(
                _CONFIG,
                descriptor={"digest": "sha256:" + "9" * 64},
            ),
            "Descriptor.digest",
        ),
        (
            _inspect_payload(
                "sha256:" + "8" * 64,
                descriptor={"digest": _MANIFEST},
            ),
            "Id",
        ),
    ),
)
def test_loaded_identity_rejects_wrong_store_identity(
    payload: list[dict[str, object]],
    expected_error: str,
) -> None:
    with pytest.raises(LoadedImageIdentityError, match=expected_error):
        validate_loaded_image_identity(
            payload,
            expected_manifest_digest=_MANIFEST,
            expected_config_digest=_CONFIG,
            expected_platform="linux/amd64",
            expected_revision=_REVISION,
        )
