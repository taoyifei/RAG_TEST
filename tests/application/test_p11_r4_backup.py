"""R4 辅助账本随产品恢复，但旧快照不能再次获得出站预算。"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import BudgetedTransport
from rag_app.product.backup import create_backup, restore_backup
from rag_app.product.live_acceptance import AcceptanceState, StepResult
from tests.product_support import build_product_harness


def test_backup_keeps_attempts_and_stages_and_blocks_snapshot_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(build_product_harness(tmp_path / "source")) as harness:
        data = harness.runtime.data_dir
        ledger = ProviderBudgetLedger(data / "provider-budget.sqlite3")
        ledger.create_campaign(
            BudgetCampaign("campaign-test", "auth-test", "public", 25, 1000)
        )
        ledger.import_history(
            "campaign-test",
            source_identity="a" * 64,
            events=[
                {
                    "event_id": "event-test",
                    "provider": "jina",
                    "operation": "embedding.query",
                    "forwarded": True,
                    "estimated_input_tokens": 19,
                    "observed_tokens": None,
                }
            ],
        )
        before = ledger.attempts("campaign-test")
        state = AcceptanceState(
            data / "p11-live-state.sqlite3", "campaign-test"
        )
        state.record(
            "aliyun_document_canary", "identity", StepResult("FAIL", "HTTP_400")
        )
        monkeypatch.setattr(
            "rag_app.product.backup._snapshot_collections",
            lambda *_args, **_kwargs: ({}, "1.18.3"),
        )
        key = tmp_path / "qdrant-test-key"
        key.write_text("synthetic-qdrant-key", encoding="utf-8")
        key.chmod(0o600)
        archive = tmp_path / "backup.tar.gz"
        create_backup(
            data_dir=data,
            output=archive,
            compatibility_manifest=Path(__file__).resolve().parents[2]
            / "compatibility-manifest.json",
            qdrant_url="http://127.0.0.1:6333",
            qdrant_api_key_file=key,
        )
    restored = tmp_path / "restored"
    restore_backup(
        archive_path=archive,
        target_data_dir=restored,
        qdrant_url="http://127.0.0.1:6333",
        qdrant_api_key_file=key,
    )
    restored_ledger = ProviderBudgetLedger(
        restored / "provider-budget.sqlite3", read_only=True
    )
    assert restored_ledger.attempts("campaign-test") == before
    restored_state = AcceptanceState(
        restored / "p11-live-state.sqlite3", "campaign-test"
    )
    assert restored_state.latest("aliyun_document_canary")["status"] == "FAIL"
    assert (restored / "provider-budget.restore-blocked").is_file()


def test_restore_before_first_binding_cannot_obtain_a_fresh_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "rag_app.product.backup._snapshot_collections",
        lambda *_args, **_kwargs: ({}, "1.18.3"),
    )
    key = tmp_path / "qdrant-test-key"
    key.write_text("synthetic-qdrant-key", encoding="utf-8")
    key.chmod(0o600)
    archive = tmp_path / "before-first-binding.tar.gz"
    with closing(build_product_harness(tmp_path / "source")) as harness:
        assert not (
            harness.runtime.data_dir / "provider-budget.sqlite3"
        ).exists()
        create_backup(
            data_dir=harness.runtime.data_dir,
            output=archive,
            compatibility_manifest=Path(__file__).resolve().parents[2]
            / "compatibility-manifest.json",
            qdrant_url="http://127.0.0.1:6333",
            qdrant_api_key_file=key,
        )
    restored = tmp_path / "restored"
    restore_backup(
        archive_path=archive,
        target_data_dir=restored,
        qdrant_url="http://127.0.0.1:6333",
        qdrant_api_key_file=key,
    )
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    with (
        httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(handler),
                ledger_path=restored / "provider-budget.sqlite3",
            )
        ) as client,
        pytest.raises(BudgetBlockedError, match="RECONCILIATION_REQUIRED"),
    ):
        client.post(
            "https://api.jina.ai/v1/embeddings", json={"input": ["synthetic"]}
        )
    assert sent == []
