"""外部主体 Token 的上游固定版必需 claims。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner


def test_signed_principal_has_scoped_user_and_valid_signature() -> None:
    signer = ExternalPrincipalSigner(
        secret="test-signer-key",  # noqa: S106 - 仅用于测试签名。
        tenant_id=7,
        deployment_id="candidate",
        clock=lambda: 1000.0,
    )
    token = signer.sign("user-42")
    header, claims, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
    expected = base64.urlsafe_b64encode(
        hmac.new(
            b"test-signer-key",
            f"{header}.{claims}".encode(),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=")
    assert signature.encode() == expected
    assert payload == {
        "aud": "weknora",
        "exp": 1120,
        "iat": 1000,
        "sub": "rdms:candidate:user-42",
        "tenant_id": 7,
    }


@pytest.mark.parametrize("user_id", ["", "a\nadmin", "x" * 200])
def test_rejects_invalid_external_subject(user_id: str) -> None:
    signer = ExternalPrincipalSigner(
        secret="test-signer-key",  # noqa: S106 - 仅用于测试签名。
        tenant_id=7,
        deployment_id="candidate",
    )
    with pytest.raises(ValueError, match="主体 ID"):
        signer.sign(user_id)
