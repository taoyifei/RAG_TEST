from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import build_industry_app_update

_ROOT = Path(__file__).parents[1]
_PACKAGE_FILES = {
    "SERVER_UPDATE_COMMANDS.txt",
    "UPDATE_MANIFEST.json",
    "app-image.tar.gz",
    "app-image.tar.gz.sha256",
    "package_selfcheck.py",
    "serving-runtime.tar.gz",
    "serving-runtime.tar.gz.sha256",
    "update-app.sh",
}


def test_serving_update_stage_requires_complete_exact_set(
    tmp_path: Path,
) -> None:
    """四文件 app-only 包不得再通过目标包 exact-set 门禁。"""
    for name in _PACKAGE_FILES:
        (tmp_path / name).write_bytes(b"placeholder")
    (tmp_path / "UPDATE_MANIFEST.json").write_text(
        json.dumps(
            {
                "branch": "Industry",
                "index_fingerprint": {"reindex_required": False},
                "target": {
                    "alias": "rag-industry-active",
                    "project": "rag-industry",
                    "service": "rag-industry-app",
                },
            }
        ),
        encoding="utf-8",
    )

    build_industry_app_update._verify_stage(tmp_path)


def test_builder_declares_serving_runtime_contract() -> None:
    """构建器必须绑定配置、runtime、UI、Trace 和兼容基线。"""
    source = (_ROOT / "scripts/build_industry_app_update.py").read_text(
        encoding="utf-8"
    )

    assert "industry-serving-update" in source
    assert "serving-runtime.tar.gz" in source
    assert "package_selfcheck.py" in source
    assert "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1" in source
    assert "same_origin_session" in source
    assert "plaintext" in source
    assert "604800" in source


def test_industry_compose_declares_target_ui_and_trace_defaults() -> None:
    """新版本化 Compose 必须显式携带已批准的 demo 合同。"""
    compose = (_ROOT / "deployment/industry/compose.yaml").read_text(
        encoding="utf-8"
    )

    assert "${RAG_TRACE_QUESTION_CAPTURE:-plaintext}" in compose
    assert "${RAG_UI_QUERY_AUTH_MODE:-same_origin_session}" in compose
    assert "${RAG_UI_COOKIE_SECURE:-false}" in compose
    assert "${RAG_UI_ALLOW_INSECURE_HTTP:-true}" in compose
    assert "${RAG_UI_SESSION_TTL_SECONDS:-1800}" in compose


def test_last_good_has_one_atomic_authority() -> None:
    """last-good 不得继续由两个可撕裂文件共同授权。"""
    library = (_ROOT / "deployment/industry/lib.sh").read_text(encoding="utf-8")
    helper = (_ROOT / "deployment/industry/serving_last_good.py").read_text(
        encoding="utf-8"
    )

    assert "last-good-pointer.json" in library
    assert "last-good-snapshots" in helper
    assert '"${backup_path}/last-good.env"' not in library
    assert '"${backup_path}/last-good.json"' not in library


def test_source_config_identity_comes_from_real_first_deploy_release() -> None:
    release = _ROOT / build_industry_app_update._SOURCE_RELEASE_PATH
    source = build_industry_app_update._load_source_release(release)
    expected = {
        name: hashlib.sha256(
            (release / "config" / name).read_bytes()
        ).hexdigest()
        for name in build_industry_app_update._CONFIG_SOURCES
    }

    assert source.config_sha256 == expected
    assert expected == build_industry_app_update._SOURCE_CONFIG_SHA256
    assert source.index_fingerprint == (
        build_industry_app_update._INDEX_FINGERPRINT
    )
    assert source.serving_fingerprint == (
        build_industry_app_update._SOURCE_SERVING_FINGERPRINT
    )
    assert build_industry_app_update._SOURCE_CONFIG_PROFILE == (
        "first-deploy-private-v1"
    )
    assert build_industry_app_update._TARGET_CONFIG_PROFILE == (
        "serving-runtime-public-config-v1"
    )
