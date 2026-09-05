"""R5 本地字段归因只读必要非秘密列，独立于预算是否已绑定。"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.product.connection_diagnostics import diagnose_configuration
from rag_app.product.models import ProviderConnectionDraft
from tests.adapters.providers.test_budget_revision import approved_revision
from tests.product_support import build_product_harness

_HOST = "api-synthetic.cn-beijing.maas.aliyuncs.com"


@pytest.fixture
def configuration(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE provider_connections (
                connection_id TEXT,display_name TEXT,provider_type TEXT,
                credential_id TEXT,endpoint_profile TEXT,
                configuration_version INTEGER,enabled INTEGER,status TEXT,
                created_at TEXT,updated_at TEXT,config_json TEXT
            );
            CREATE TABLE provider_credentials (
                credential_id TEXT,provider_type TEXT,key_version INTEGER,
                status TEXT,ciphertext TEXT
            );
            CREATE TABLE retrieval_profile_revisions (
                profile_revision_id TEXT,primary_connection_id TEXT,
                primary_embedding_model TEXT,primary_dimension INTEGER,
                primary_document_policy_json TEXT,
                primary_query_policy_json TEXT,
                standby_connection_id TEXT,standby_embedding_model TEXT,
                standby_dimension INTEGER,standby_document_policy_json TEXT,
                standby_query_policy_json TEXT,primary_resolved_json TEXT,
                standby_resolved_json TEXT,retrieval_policy_json TEXT,
                evidence_policy_json TEXT
            );
        """)
        database.execute(
            "INSERT INTO provider_connections VALUES "
            "('conn_synthetic','safe','aliyun-model-studio','cred_synthetic',"
            "'default',1,1,'configured','','',?)",
            (
                json.dumps(
                    {
                        "workspace_id": "ws-private-shape-only",
                        "api_host": _HOST,
                        "endpoint_mode": "workspace_host",
                        "region": "cn-beijing",
                        "request_budget": 5,
                        "token_budget": 4096,
                    }
                ),
            ),
        )
        database.execute(
            "INSERT INTO provider_credentials VALUES "
            "('cred_synthetic','aliyun-model-studio',1,'configured',?)",
            ("never-select-this-material",),
        )
    return {
        "data_dir": str(tmp_path),
        "aliyun_connection_id": "conn_synthetic",
        "ledger_path": str(tmp_path / "provider-budget.sqlite3"),
        "campaign_id": "campaign-synthetic",
        "authorization_id": "approved-v1",
        "candidate_identity": "synthetic-candidate",
    }


def _change(config: dict[str, object], **changes: object) -> None:
    with sqlite3.connect(
        Path(str(config["data_dir"])) / "universal-rag.sqlite3"
    ) as database:
        values = json.loads(
            database.execute(
                "SELECT config_json FROM provider_connections"
            ).fetchone()[0]
        )
        values.update(changes)
        database.execute(
            "UPDATE provider_connections SET config_json=?",
            (json.dumps(values),),
        )


def _codes(result: dict[str, Any]) -> set[str]:
    return {item["reason_code"] for item in result["issues"]}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("api_host", None, "ALIYUN_API_HOST_REQUIRED"),
        ("api_host", "https://user@" + _HOST, "ALIYUN_API_HOST_FORMAT_INVALID"),
        ("api_host", "https://wrong.example", "ALIYUN_API_HOST_NOT_ALLOWED"),
        ("region", "cn-shanghai", "ALIYUN_REGION_MISMATCH"),
        ("region", None, "ALIYUN_REGION_MISMATCH"),
        ("endpoint_mode", None, "ALIYUN_ENDPOINT_MODE_REQUIRED"),
        ("endpoint_mode", "other", "ALIYUN_ENDPOINT_MODE_INVALID"),
        ("workspace_id", "bad\nvalue", "ALIYUN_WORKSPACE_FORMAT_INVALID"),
    ],
)
def test_specific_field_causes(
    configuration: dict[str, object],
    field: str,
    value: object,
    reason: str,
):
    _change(configuration, **{field: value})
    report = diagnose_configuration(configuration)
    assert report["endpoint_contract"] == "BLOCKED"
    assert reason in _codes(report)
    assert "CAMPAIGN_BINDING_REQUIRED" in _codes(report)
    assert "CONNECTION_OR_PROFILE_INVALID" not in _codes(report)
    assert _HOST not in json.dumps(report)
    assert "ws-private-shape-only" not in json.dumps(report)


def test_missing_mode_and_host_are_both_identified(
    configuration: dict[str, object],
):
    _change(configuration, endpoint_mode=None, api_host=None)
    report = diagnose_configuration(configuration)
    assert _codes(report) >= {
        "ALIYUN_ENDPOINT_MODE_REQUIRED",
        "ALIYUN_API_HOST_REQUIRED",
    }
    assert (
        report["connections"]["aliyun_connection_id"]["workspace_shape_valid"]
        is True
    )


def test_campaign_is_independent_from_valid_connection(
    configuration: dict[str, object],
):
    report = diagnose_configuration(configuration)
    assert report["status"] == "BLOCKED"
    assert report["endpoint_contract"] == "PASS"
    assert report["connection_configuration"] == "PASS"
    assert report["campaign_binding"] == "BLOCKED"
    assert report["live_allowed"] is False
    ledger = ProviderBudgetLedger(Path(str(configuration["ledger_path"])))
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="campaign-synthetic",
            authorization_id="approved-v1",
            scope="p11-public-synthetic-v1",
            request_limit=25,
            estimated_token_limit=1000,
        )
    )
    assert (
        diagnose_configuration(configuration)["campaign_binding"] == "BLOCKED"
    )
    ledger.activate_campaign("campaign-synthetic")
    assert diagnose_configuration(configuration)["status"] == "PASS"
    configuration["authorization_id"] = "different"
    assert "CAMPAIGN_AUTHORIZATION_MISMATCH" in _codes(
        diagnose_configuration(configuration)
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "expires_at",
            "2020-01-01T00:00:00+00:00",
            "BUDGET_REVISION_TIME_INVALID",
        ),
        ("scope", "other-synthetic", "BUDGET_REVISION_SCOPE_MISMATCH"),
        ("previous_revision_id", "missing", "BUDGET_REVISION_CHAIN_MISMATCH"),
    ],
)
def test_invalid_authorization_revision_only_blocks_campaign(
    configuration: dict[str, object],
    field: str,
    value: str,
    reason: str,
):
    ledger = ProviderBudgetLedger(Path(str(configuration["ledger_path"])))
    campaign = BudgetCampaign(
        campaign_id="campaign-synthetic",
        authorization_id="approved-v1",
        scope="p11-public-synthetic-v1",
        request_limit=25,
        estimated_token_limit=1000,
    )
    ledger.create_campaign(campaign)
    ledger.activate_campaign(campaign.campaign_id)
    ledger.apply_revision(
        approved_revision(
            campaign, request_limit=30, estimated_token_limit=2000
        ),
        admin_session_id="sess_synthetic",
    )
    assert diagnose_configuration(configuration)["campaign_binding"] == "PASS"
    # 仅在隔离账本模拟时间流逝或损坏的历史记录，真实入口不能修改旧修订。
    with sqlite3.connect(ledger.path) as database:
        values = json.loads(
            database.execute(
                "SELECT configuration FROM provider_budget_revisions"
            ).fetchone()[0]
        )
        values[field] = value
        database.execute(
            "UPDATE provider_budget_revisions SET configuration=?",
            (json.dumps(values),),
        )
    before = ledger.path.read_bytes()
    report = diagnose_configuration(configuration)
    assert reason in _codes(report)
    assert report["status"] == "BLOCKED"
    assert report["campaign_binding"] == "BLOCKED"
    assert report["endpoint_contract"] == "PASS"
    assert report["connection_configuration"] == "PASS"
    assert ledger.path.read_bytes() == before


def test_read_boundary_denies_secret_columns_and_all_mutations(
    configuration: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
):
    connect = sqlite3.connect
    reads: list[tuple[str | None, str | None]] = []

    def authorize(
        action: int,
        table: str | None,
        column: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        del database, trigger
        assert action in {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        if action == sqlite3.SQLITE_READ:
            reads.append((table, column))
            assert column != "ciphertext"
        return sqlite3.SQLITE_OK

    def guarded(*args: object, **kwargs: object) -> sqlite3.Connection:
        assert str(args[0]).endswith("?mode=ro")
        database = connect(*args, **kwargs)
        database.set_authorizer(authorize)
        return database

    monkeypatch.setattr(sqlite3, "connect", guarded)
    report = diagnose_configuration(configuration)
    assert report["connection_configuration"] == "PASS"
    assert report["http_requests"] == 0
    assert report["secret_decryption"] is False
    assert report["migrations"] is False
    assert reads
    assert {
        column for table, column in reads if table == "provider_credentials"
    } == {"credential_id", "provider_type", "status", "key_version"}


def test_missing_connection_credential_and_disabled_have_distinct_causes(
    configuration: dict[str, object],
):
    with sqlite3.connect(
        Path(str(configuration["data_dir"])) / "universal-rag.sqlite3"
    ) as database:
        database.execute("DELETE FROM provider_credentials")
        database.execute("UPDATE provider_connections SET enabled=0")
    report = diagnose_configuration(configuration)
    assert _codes(report) >= {
        "CREDENTIAL_METADATA_MISSING",
        "CONNECTION_DISABLED",
    }
    configuration["aliyun_connection_id"] = "missing"
    assert "CONNECTION_NOT_FOUND" in _codes(
        diagnose_configuration(configuration)
    )


def test_wrong_profile_and_invalid_policy_are_distinct(
    configuration: dict[str, object],
):
    configuration["source_profile_revision_id"] = "profile-synthetic"
    assert "SOURCE_PROFILE_NOT_FOUND" in _codes(
        diagnose_configuration(configuration)
    )
    with sqlite3.connect(
        Path(str(configuration["data_dir"])) / "universal-rag.sqlite3"
    ) as database:
        database.execute(
            "INSERT INTO retrieval_profile_revisions "
            "(profile_revision_id,standby_connection_id,standby_embedding_model,"
            "standby_dimension) VALUES "
            "('profile-synthetic','different','qwen3.7-text-embedding',1024)"
        )
    assert "PROFILE_CONNECTION_MISMATCH" in _codes(
        diagnose_configuration(configuration)
    )
    with sqlite3.connect(
        Path(str(configuration["data_dir"])) / "universal-rag.sqlite3"
    ) as database:
        database.execute(
            "UPDATE retrieval_profile_revisions SET standby_connection_id="
            "'conn_synthetic',standby_query_policy_json=?",
            (json.dumps({"query_instruct": {"secret": "do-not-copy"}}),),
        )
    report = diagnose_configuration(configuration)
    assert "PROFILE_POLICY_INVALID" in _codes(report)
    assert "do-not-copy" not in json.dumps(report)
    assert "input" not in json.dumps(report)


def test_sql_failure_is_not_misreported_as_user_credential_error(
    configuration: dict[str, object],
):
    with sqlite3.connect(
        Path(str(configuration["data_dir"])) / "universal-rag.sqlite3"
    ) as database:
        database.execute("DROP TABLE provider_connections")
    report = diagnose_configuration(configuration)
    assert report["status"] == "FAIL"
    assert report["safe_error_type"] == "OperationalError"
    assert "DIAGNOSTIC_EXECUTION_FAILED" in _codes(report)
    assert "CREDENTIAL_METADATA_MISSING" not in _codes(report)


def test_invalid_credential_version_and_ledger_path(
    configuration: dict[str, object],
):
    with sqlite3.connect(
        Path(str(configuration["data_dir"])) / "universal-rag.sqlite3"
    ) as database:
        database.execute("UPDATE provider_credentials SET key_version=0")
    configuration["ledger_path"] = str(
        Path(str(configuration["data_dir"])) / "other.db"
    )
    assert _codes(diagnose_configuration(configuration)) >= {
        "CREDENTIAL_VERSION_INVALID",
        "PRODUCT_LEDGER_PATH_MISMATCH",
    }


def test_edit_original_missing_mode_normalizes_without_secret_or_probe(
    tmp_path: Path,
):
    harness = build_product_harness(tmp_path)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio", "synthetic-retained-material"
        )
        saved = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="合成连接",
                provider_type="aliyun-model-studio",
                credential_id=credential.credential_id,
                workspace_id="ws-synthetic",
                api_host=_HOST,
                region="cn-beijing",
            )
        )
        with harness.runtime.connections.transaction(write=True) as database:
            database.execute(
                "UPDATE provider_connections SET config_json="
                "json_remove(config_json,'$.endpoint_mode','$.api_host')"
            )
        original = harness.runtime.control.get_connection(saved.connection_id)
        assert original.endpoint_mode == ""
        assert original.api_host is None
        response = harness.client.patch(
            "/api/v1/provider-connections/" + saved.connection_id,
            headers=harness.write_headers,
            json={
                "expected_version": saved.configuration_version,
                "endpoint_mode": "workspace_host",
                "api_host": _HOST + ":443/",
            },
        )
        assert response.status_code == 200
        assert response.json()["api_host"] == "https://" + _HOST
        assert response.json()["credential_id"] == credential.credential_id
        assert (
            harness.runtime.credentials.get(credential.credential_id)
            == credential
        )
        assert (
            harness.runtime.control.list_validations(saved.connection_id) == ()
        )
        assert harness.runtime.providers._clients == {}
    finally:
        harness.close()
