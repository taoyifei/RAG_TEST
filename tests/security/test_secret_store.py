"""页面托管与环境托管 Credential 的安全回归。"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from rag_app.product.crypto import (
    SecretAad,
    SecretCipher,
    initialize_master_key,
    initialize_product_secret_bundle,
    load_master_key,
)
from rag_app.product.models import ProviderConnectionDraft
from tests.product_support import build_product_harness


def test_aes_gcm_nonce_aad_and_sqlite_plaintext_boundary(
    tmp_path: Path,
) -> None:
    key = initialize_master_key(tmp_path / "standalone-key")
    cipher = SecretCipher(key)
    aad = SecretAad("cred_one", "jina", "api_key", 1)
    encrypted_one = cipher.encrypt("synthetic-secret", aad=aad)
    encrypted_two = cipher.encrypt("synthetic-secret", aad=aad)

    assert encrypted_one[1] != encrypted_two[1]
    assert cipher.decrypt(*encrypted_one, aad=aad) == "synthetic-secret"
    with pytest.raises(InvalidTag):
        cipher.decrypt(
            *encrypted_one,
            aad=SecretAad("cred_two", "jina", "api_key", 1),
        )

    harness = build_product_harness(tmp_path / "runtime")
    try:
        credential_value = "database-only-synthetic-value"
        created = harness.client.post(
            "/api/v1/provider-credentials",
            headers=harness.write_headers,
            json={
                "provider_type": "jina",
                "source": "database_encrypted",
                "secret_value": credential_value,
            },
        )
        created.raise_for_status()
        listed = harness.client.get("/api/v1/provider-credentials")
        database = (
            harness.runtime.settings.data_dir / "universal-rag.sqlite3"
        ).read_bytes()

        assert credential_value.encode() not in database
        assert credential_value not in created.text
        assert credential_value not in listed.text
        assert "encrypted_payload" not in listed.text
        assert "nonce" not in listed.text
    finally:
        harness.close()


def test_master_key_file_contract_rejects_missing_mode_and_symlink(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError):
        load_master_key(missing)

    insecure = tmp_path / "insecure"
    insecure.write_bytes(bytes(range(32)))
    insecure.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_master_key(insecure)

    target = tmp_path / "target"
    target.write_bytes(bytes(reversed(range(32))))
    target.chmod(0o600)
    symlink = tmp_path / "link"
    symlink.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_master_key(symlink)


def test_product_secret_bundle_is_private_and_output_safe(
    tmp_path: Path,
) -> None:
    bundle = initialize_product_secret_bundle(tmp_path)
    paths = tuple(tmp_path.iterdir())
    bootstrap = (tmp_path / "admin-bootstrap-token").read_text(
        encoding="utf-8"
    )
    qdrant_key = (tmp_path / "qdrant-api-key").read_text(encoding="utf-8")
    config = (tmp_path / "qdrant.yaml").read_text(encoding="utf-8")

    assert {path.name for path in paths} == {
        "admin-bootstrap-token",
        "master-key",
        "qdrant-api-key",
        "qdrant.yaml",
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert qdrant_key in config
    assert bootstrap not in repr(bundle)
    assert qdrant_key not in repr(bundle)
    assert bundle.master_key_id.startswith("sha256:")
    assert bundle.bootstrap_token_id.startswith("sha256:")
    assert bundle.qdrant_api_key_id.startswith("sha256:")

    with pytest.raises(FileExistsError):
        initialize_product_secret_bundle(tmp_path)


def test_environment_managed_credential_stores_only_variable_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_product_harness(tmp_path)
    environment_name = "RAG_TEST_JINA_ENVIRONMENT"
    value = "environment-synthetic-value"
    monkeypatch.setenv(environment_name, value)
    try:
        created = harness.runtime.credentials.create_environment(
            "jina", environment_name
        )
        resolved, version = harness.runtime.credentials.resolve(
            created.credential_id
        )
        with sqlite3.connect(
            harness.runtime.settings.data_dir / "universal-rag.sqlite3"
        ) as connection:
            stored = connection.execute(
                "SELECT encrypted_payload FROM provider_credentials "
                "WHERE credential_id=?",
                (created.credential_id,),
            ).fetchone()

        assert created.configured is True
        assert (resolved, version) == (value, 1)
        assert stored == (environment_name,)
        assert (
            value.encode()
            not in (
                harness.runtime.settings.data_dir / "universal-rag.sqlite3"
            ).read_bytes()
        )
    finally:
        harness.close()


def test_secret_is_absent_from_logs_trace_repr_and_safe_responses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential_value = "synthetic-observability-boundary-value"
    caplog.set_level(logging.DEBUG)
    harness = build_product_harness(tmp_path)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "jina", credential_value
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="安全观测连接",
                provider_type="jina",
                credential_id=credential.credential_id,
            )
        )
        validation = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        responses = (
            harness.client.get("/api/v1/provider-credentials").text,
            harness.client.get("/api/v1/provider-connections").text,
            json.dumps(validation.model_dump(mode="json")),
            repr(credential),
            caplog.text,
        )
        database_bytes = b"".join(
            path.read_bytes()
            for path in harness.runtime.settings.data_dir.rglob("*.sqlite3")
        )

        assert all(credential_value not in value for value in responses)
        assert credential_value.encode() not in database_bytes
    finally:
        harness.close()
