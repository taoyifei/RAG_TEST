"""SSO 有限入口注册表与 fail-fast 配置回归。"""

from __future__ import annotations

import json

import pytest

from rag_app.wanshitong.sso_settings import SsoSettings


def _environment() -> dict[str, str]:
    return {
        "RAG_WANSHITONG_AUTH_MODE": "sso",
        "RAG_WANSHITONG_SSO_DEPLOYMENT_ID": "candidate_8289",
        "RAG_WANSHITONG_SSO_ENTRIES": json.dumps(
            [
                {
                    "id": "internal",
                    "origin": "http://10.242.180.54:8289",
                    "authorize_url": (
                        "http://10.242.180.50:8080/sso/authorize"
                    ),
                },
                {
                    "id": "external",
                    "origin": "https://rd.example.test",
                    "authorize_url": (
                        "https://rd.example.test/sso/authorize"
                    ),
                },
            ]
        ),
        "RAG_WANSHITONG_SSO_VALIDATE_URL": (
            "http://10.242.180.50:21000/sso/validate"
        ),
        "RAG_WANSHITONG_SSO_CLIENT_ID": "kb",
        "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE": (
            "/run/rag-secrets/sso-client-secret"
        ),
    }


def test_sso_settings_freeze_exact_callbacks_and_entry_lookup() -> None:
    settings = SsoSettings.from_environment(
        _environment(), root_path="/kb"
    )

    assert settings.enabled is True
    assert settings.deployment_id == "candidate_8289"
    assert settings.entry_for_origin(
        "http://10.242.180.54:8289"
    ).callback_url == (
        "http://10.242.180.54:8289/kb/sso/callback"
    )
    assert settings.entry_by_id("external").callback_url == (
        "https://rd.example.test/kb/sso/callback"
    )


def test_anonymous_mode_does_not_require_sso_inputs() -> None:
    settings = SsoSettings.from_environment({}, root_path="")

    assert settings.enabled is False
    assert settings.auth_mode == "anonymous"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"RAG_WANSHITONG_AUTH_MODE": "oidc"}, "AUTH_MODE"),
        ({"RAG_WANSHITONG_SSO_ENTRIES": "[]"}, "非空"),
        (
            {
                "RAG_WANSHITONG_SSO_VALIDATE_URL": (
                    "http://idp.test/login"
                )
            },
            "/sso/validate",
        ),
    ],
)
def test_sso_settings_reject_invalid_contracts(
    mutation: dict[str, str], message: str
) -> None:
    environment = _environment()
    environment.update(mutation)

    with pytest.raises(ValueError, match=message):
        SsoSettings.from_environment(environment, root_path="/kb")


def test_sso_settings_require_fixed_kb_root_and_known_origin() -> None:
    with pytest.raises(ValueError, match="RAG_ROOT_PATH"):
        SsoSettings.from_environment(_environment(), root_path="")

    settings = SsoSettings.from_environment(
        _environment(), root_path="/kb"
    )
    with pytest.raises(ValueError, match="未登记"):
        settings.entry_for_origin("http://attacker.test")
