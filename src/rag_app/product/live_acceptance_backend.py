"""复用页面托管连接的 P11 执行器；本地检查不加载任何 Secret。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import cast
from unittest.mock import patch

import httpx

from evaluation.p11_pilot_runtime import run_pilot
from rag_app.adapters.providers.budget_authorization import (
    bind_existing_product_campaign,
)
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import (
    provider_budget_fault,
    provider_budget_scope,
)
from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.application.provider_health import ProviderCircuitBreaker
from rag_app.composition.product_runtime import (
    ProductRuntime,
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.core.errors import RagError
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    CircuitKey,
    CircuitState,
    RetrievalPolicy,
    SearchAnswerResult,
)
from rag_app.core.policies import CircuitBreakerPolicy
from rag_app.product.control_store import ProductControlStore
from rag_app.product.credential_store import CredentialStore
from rag_app.product.crypto import SecretCipher, load_master_key
from rag_app.product.live_acceptance import AcceptanceState, StepResult
from rag_app.product.live_acceptance_payloads import (
    DOCUMENT_NAME,
    QUERIES,
    QUERY_LIMIT,
    approved_payload_contracts,
    functional_document,
)
from rag_app.product.models import (
    CredentialSummary,
    ImpactKind,
    RetrievalProfileDraft,
)
from rag_app.product.provider_runtime import ProviderRuntimeRegistry
from rag_app.product.quality import QualityKind, QualityValidationRecord
from rag_app.product.resolved_profile import (
    ResolvedEmbeddingSpec,
    resolve_embedding,
)
from rag_app.product.verification import (
    endpoint_identity,
    profile_specs,
    validation_is_current,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_JINA_KEY = CircuitKey(
    "jina-embedding", "embedding", "jina-embeddings-v5-text-small"
)
_DIMENSION = 1024
_AUTHORIZED_REQUEST_LIMIT = 25
_AUTHORIZED_TOKEN_LIMIT = 1000
_AUTHORIZED_PROVIDER_TOKEN_LIMIT = 600


class _MetadataConnections(SqliteConnectionFactory):
    """本地检查禁止迁移、写入 PRAGMA 或修改产品数据库。"""

    def connect(self) -> sqlite3.Connection:
        """以只读模式连接现有数据库。

        Args:
            无参数；使用已核对的数据库路径。

        Returns:
            禁止写入的 SQLite 连接。

        """
        connection = sqlite3.connect(
            self.database_path.as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        return connection


class _MetadataCredentials(CredentialStore):
    """本地检查只 SELECT Credential 摘要列，密文也不加载。"""

    def get(self, credential_id: str) -> CredentialSummary:
        """读取不含密文的 Credential 元数据。

        Args:
            credential_id: 既有页面凭据引用。

        Returns:
            安全摘要。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT credential_id,provider_type,source,masked_hint,"
                "key_version,status,created_at,updated_at FROM "
                "provider_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Credential 元数据不存在。")
        return CredentialSummary.model_validate(
            {**dict(row), "configured": row["status"] == "configured"}
        )


class ProductAcceptanceBackend:
    """只有获准执行在线阶段时才创建产品 Runtime。"""

    def __init__(
        self, config: dict[str, object], state: AcceptanceState
    ) -> None:
        self.config = config
        if config.get("data_dir") and not config.get("ledger_path"):
            config["ledger_path"] = str(
                Path(str(config["data_dir"])) / "provider-budget.sqlite3"
            )
        self.state = state
        self.runtime: ProductRuntime | None = None
        self.provider_registry: ProviderRuntimeRegistry | None = None
        self.control: ProductControlStore | None = None
        self.configuration_error: str | None = None
        self.clock = 0.0
        self.circuits: list[ProviderCircuitBreaker] = []
        self.fault_mode: str | None = None
        self.observed_half_open = False
        self._load_metadata()

    def _load_metadata(self) -> None:
        required = (
            "data_dir",
            "ledger_path",
            "campaign_id",
            "authorization_id",
            "candidate_identity",
        )
        missing = [name for name in required if not self.config.get(name)]
        if missing:
            self.configuration_error = "MISSING_CONFIG:" + ",".join(missing)
            return
        database = (
            Path(str(self.config["data_dir"])).resolve()
            / "universal-rag.sqlite3"
        )
        if not database.is_file():
            self.configuration_error = "PRODUCT_DATABASE_NOT_FOUND"
            return
        # 只选公开元数据，既不 SELECT 密文，也不调用 Credential.resolve。
        try:
            with sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True
            ) as db:
                db.execute(
                    "SELECT configuration_version FROM provider_connections "
                    "LIMIT 1"
                ).fetchall()
            factory = _MetadataConnections(database)
            self.control = ProductControlStore(
                factory, _MetadataCredentials(factory, None)
            )
        except sqlite3.DatabaseError:
            self.configuration_error = "PRODUCT_SCHEMA_REQUIRES_UPGRADE"

    def identity(self, step: str) -> str:
        """按操作身份失效；另一供应商变更不会强制重验 Jina。

        Args:
            step: 选中的阶段。

        Returns:
            当前阶段的安全身份摘要。

        """
        operation_ids = cast(
            dict[str, object], self.config.get("operation_identities", {})
        )
        identity: dict[str, object] = {
            "campaign_id": self.config.get("campaign_id"),
            "authorization_id": self.config.get("authorization_id"),
            "candidate": None
            if step == "config_check"
            else operation_ids.get(step, self.config.get("candidate_identity")),
            "payload_set": "p11-public-synthetic-v1",
            "step": step,
        }
        if self.control is None or step == "config_check":
            identity["configuration_error"] = self.configuration_error
            return canonical_sha256(identity)
        try:
            if step in {
                "jina_connection",
                "aliyun_document_canary",
                "aliyun_query_canary",
            }:
                identity["validation_day"] = (
                    datetime.now(UTC).date().isoformat()
                )
            names = (
                ("jina_connection_id",)
                if step == "jina_connection"
                else ("aliyun_connection_id",)
                if step.startswith("aliyun_")
                else ("jina_connection_id", "aliyun_connection_id")
            )
            for name in names:
                if not self.config.get(name):
                    identity[name] = "CONNECTION_NOT_CONFIGURED"
                    continue
                connection = self.control.get_connection(str(self.config[name]))
                identity[name] = {
                    "connection": connection.connection_id,
                    "configuration": connection.configuration_version,
                    "credential": connection.credential_id,
                    "key_version": self.control.credential_version(
                        connection.credential_id
                    ),
                    "endpoint": endpoint_identity(connection),
                }
            specs = [
                self._spec(name) for name in names if self.config.get(name)
            ]
            identity["policies"] = (
                [
                    spec.policy_identity(
                        "embedding.document"
                        if step == "aliyun_document_canary"
                        else "embedding.query"
                    )
                    for spec in specs
                ]
                if step.startswith("aliyun_")
                else [spec.model_dump(mode="json") for spec in specs]
            )
        except (RagError, ValueError, sqlite3.DatabaseError):
            identity["configuration_error"] = "CONNECTION_OR_PROFILE_INVALID"
        return canonical_sha256(identity)

    def _spec(self, key: str) -> ResolvedEmbeddingSpec:
        if self.control is None:
            raise ValueError("连接元数据未就绪。")
        connection = self.control.get_connection(str(self.config[key]))
        source = self.config.get("source_profile_revision_id")
        if source:
            profile = self.control.get_profile(str(source))
            expected = (
                profile.primary_connection_id
                if key == "jina_connection_id"
                else profile.standby_connection_id
            )
            if expected != connection.connection_id:
                raise ValueError("源方案没有引用指定的页面连接。")
            specs = profile_specs(profile, self.control.get_connection)
            spec = specs[0 if key == "jina_connection_id" else 1]
        else:
            model = (
                "jina-embeddings-v5-text-small"
                if key == "jina_connection_id"
                else "qwen3.7-text-embedding"
            )
            spec = resolve_embedding(connection, model, _DIMENSION, {}, {})
        model = (
            "jina-embeddings-v5-text-small"
            if key == "jina_connection_id"
            else "qwen3.7-text-embedding"
        )
        if spec.model != model or spec.dimension != _DIMENSION:
            raise ValueError("P11 模型和维度契约不匹配。")
        return spec

    def execute(self, step: str) -> StepResult:
        """执行选中步骤并将可诊断失败与预算阻断分别保存。

        Args:
            step: 选中的阶段。

        Returns:
            真实执行、失败或受阻证据。

        """
        if step == "config_check":
            return self._check()
        try:
            return self._execute_bound(step)
        except BudgetBlockedError as error:
            return StepResult(
                "BLOCKED",
                error.reason,
                {
                    "budget_reason": error.reason,
                    "minimum_additional": error.minimum_additional,
                },
            )
        except (RagError, ValueError, OSError, sqlite3.DatabaseError) as error:
            # 不复制异常正文：它可能包含 URL、底层响应或授权材料。
            return StepResult(
                "FAIL",
                "EXECUTION_FAILED",
                {"safe_error_type": type(error).__name__},
            )

    def _execute_bound(self, step: str) -> StepResult:
        started_identity = self.identity(step)
        ledger = ProviderBudgetLedger(Path(str(self.config["ledger_path"])))
        campaign_id = str(self.config["campaign_id"])
        campaign = ledger.campaign(campaign_id)
        if campaign.authorization_id != self.config[
            "authorization_id"
        ] or campaign.scope != self.config.get(
            "scope", "p11-public-synthetic-v1"
        ):
            raise BudgetBlockedError("CAMPAIGN_AUTHORIZATION_MISMATCH")
        active = ledger.active_campaign()
        if active is None or active.campaign_id != campaign_id:
            raise BudgetBlockedError("CAMPAIGN_BINDING_REQUIRED")
        if (
            step.startswith("aliyun_")
            and campaign.step_request_limits.get(step) != 1
        ):
            raise BudgetBlockedError("CANARY_REQUIRES_SINGLE_REQUEST_LIMIT")
        before = {item["attempt_id"] for item in ledger.attempts(campaign_id)}
        with (
            self._background_budget(step),
            provider_budget_fault(self._local_blocker),
            provider_budget_scope(
                ledger,
                campaign_id=campaign_id,
                authorization_id=str(self.config["authorization_id"]),
                scope=str(self.config.get("scope", "p11-public-synthetic-v1")),
                step_id=step,
            ),
        ):
            result = self._execute_online(step)
        attempts = [
            item
            for item in ledger.attempts(campaign_id)
            if item["attempt_id"] not in before
        ]
        evidence = {
            **result.evidence,
            "attempts": attempts,
            "budget": ledger.summary(campaign_id),
            "new_forwarded_http": sum(
                int(item["forwarded"]) for item in attempts
            ),
            "new_locally_blocked": sum(
                int(item["locally_blocked"]) for item in attempts
            ),
        }
        denied = [
            item
            for item in attempts
            if item["status"]
            in {
                "BLOCKED_BUDGET",
                "PAYLOAD_NOT_APPROVED",
                "REQUEST_IDENTITY_NOT_APPROVED",
                "AUTHORIZATION_ID_MISMATCH",
                "AUTHORIZATION_SCOPE_MISMATCH",
            }
        ]
        if denied:
            latest = denied[-1]
            if latest["status"] == "BLOCKED_BUDGET":
                evidence["minimum_additional"] = ledger.minimum_additional(
                    campaign_id, str(latest["attempt_id"])
                )
            return StepResult("BLOCKED", str(latest["status"]), evidence)
        if self.identity(step) != started_identity:
            return StepResult(
                "BLOCKED", "IDENTITY_CHANGED_DURING_STEP", evidence
            )
        if (
            result.status == "PASS"
            and step in {"primary_query", "standby_failover", "recovery"}
            and not any(
                item["forwarded"] and item["operation"] == "embedding.query"
                for item in attempts
            )
        ):
            return StepResult("FAIL", "CACHE_MASKED_NETWORK_PATH", evidence)
        self._record_functional_quality(step, result, evidence)
        return StepResult(result.status, result.reason, evidence)

    def _record_functional_quality(
        self, step: str, result: StepResult, evidence: dict[str, object]
    ) -> None:
        if self.runtime is None or step not in {"dual_index", "recovery"}:
            return
        profile_id = self.state.resource("profile_revision_id")
        if profile_id is None:
            return
        control = self.runtime.control
        profile = control.get_profile(profile_id)
        kind: QualityKind
        if step == "dual_index":
            validations = control.profile_validations(profile_id)
            current = bool(validations) and all(
                item is not None
                and item.status == "succeeded"
                and item.validation_mode == "live"
                and item.http_category == "live_200"
                for item in validations.values()
            )
            kind = "provider_connectivity_verified"
            gates = {"required_operations": current}
        else:
            primary = self.state.latest("primary_query") or {}
            standby = self.state.latest("standby_failover") or {}
            kind = "dual_slot_function_verified"
            gates = {
                "primary": primary.get("status") == "PASS",
                "standby": standby.get("status") == "PASS",
                "failover": result.status == "PASS" and self.observed_half_open,
                "isolation": result.status == "PASS"
                and evidence.get("active_index_revision_id")
                == self.state.resource("revision_id"),
            }
        control.quality.record(
            QualityValidationRecord(
                profile_revision_id=profile_id,
                kind=kind,
                validation_mode="live",
                run_id=self.state.campaign_id + ":" + step,
                dataset_sha256=canonical_sha256(
                    {"document": DOCUMENT_NAME, "queries": QUERIES}
                ),
                artifact_sha256=canonical_sha256(evidence),
                index_fingerprint=profile.index_semantic_fingerprint,
                serving_fingerprint=profile.serving_fingerprint,
                gates=gates,
            )
        )

    def _execute_online(self, step: str) -> StepResult:
        names = (
            ("aliyun_connection_id",)
            if step.startswith("aliyun_")
            else ("jina_connection_id",)
            if step == "jina_connection"
            else ("jina_connection_id", "aliyun_connection_id")
        )
        if any(not self.config.get(name) for name in names):
            return StepResult("BLOCKED", "REQUESTED_CONNECTION_NOT_CONFIGURED")
        if step.startswith("aliyun_"):
            return self._canary(step)
        if step == "jina_connection":
            return self._jina()
        runtime = self._runtime()
        if runtime.providers.test_only_transport:
            return StepResult("BLOCKED", "MOCK_TRANSPORT_IS_NOT_LIVE")
        operations = {
            "dual_index": self._dual_index,
            "citation_quality": self._quality,
        }
        handler = operations.get(step)
        return self._query(step) if handler is None else handler()

    @contextmanager
    def _background_budget(self, step: str) -> Iterator[None]:
        values = {
            "RAG_PROVIDER_BUDGET_LEDGER": str(self.config["ledger_path"]),
            "RAG_PROVIDER_BUDGET_CAMPAIGN_ID": str(self.config["campaign_id"]),
            "RAG_PROVIDER_BUDGET_AUTHORIZATION_ID": str(
                self.config["authorization_id"]
            ),
            "RAG_PROVIDER_BUDGET_SCOPE": str(
                self.config.get("scope", "p11-public-synthetic-v1")
            ),
            "RAG_PROVIDER_BUDGET_STEP_ID": step,
        }
        with patch.dict(os.environ, values):
            yield

    def _check(self) -> StepResult:
        if self.configuration_error or self.control is None:
            return StepResult(
                "BLOCKED", self.configuration_error or "CONFIGURATION_REQUIRED"
            )
        try:
            expected_ledger = (
                Path(str(self.config["data_dir"])).resolve()
                / "provider-budget.sqlite3"
            )
            if (
                Path(str(self.config["ledger_path"])).resolve()
                != expected_ledger
            ):
                return StepResult("BLOCKED", "PRODUCT_LEDGER_PATH_MISMATCH")
            for name in ("jina_connection_id", "aliyun_connection_id"):
                if not self.config.get(name):
                    continue
                connection = self.control.get_connection(str(self.config[name]))
                endpoint_identity(connection)
                self._spec(name)
                if not connection.enabled:
                    return StepResult("BLOCKED", "CONNECTION_DISABLED")
                credential = self.control.credential_version(
                    connection.credential_id
                )
                if credential < 1:
                    raise ValueError("Credential 版本无效。")
            if not Path(str(self.config["ledger_path"])).is_file():
                return StepResult("BLOCKED", "PERSISTENT_CAMPAIGN_REQUIRED")
        except (RagError, ValueError, sqlite3.DatabaseError):
            return StepResult("BLOCKED", "CONNECTION_OR_PROFILE_INVALID")
        budget = self.budget_snapshot()
        return StepResult(
            "PASS" if budget["status"] == "PASS" else "BLOCKED",
            "LOCAL_CONFIGURATION_VALID"
            if budget["status"] == "PASS"
            else str(budget["reason"]),
            {"http_requests": 0, "secret_decryption": False},
        )

    def budget_snapshot(self) -> dict[str, object]:
        """读取脱敏 campaign 与 attempts，不初始化或改变账本。

        Args:
            无参数；使用安全配置中的既有账本。

        Returns:
            当前累计使用记录或无法读取的原因。

        """
        path = self.config.get("ledger_path")
        campaign_id = str(self.config.get("campaign_id", ""))
        if not path or not Path(str(path)).is_file() or not campaign_id:
            return {
                "status": "BLOCKED",
                "reason": "PERSISTENT_CAMPAIGN_REQUIRED",
            }
        try:
            ledger = ProviderBudgetLedger(Path(str(path)), read_only=True)
            campaign = ledger.campaign(campaign_id)
            if campaign.authorization_id != self.config.get(
                "authorization_id"
            ) or campaign.scope != self.config.get(
                "scope", "p11-public-synthetic-v1"
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "CAMPAIGN_AUTHORIZATION_MISMATCH",
                }
            return {
                "status": "PASS",
                "summary": ledger.summary(campaign_id),
                "attempts": ledger.attempts(campaign_id),
            }
        except (BudgetBlockedError, ValueError, sqlite3.DatabaseError):
            return {
                "status": "BLOCKED",
                "reason": "PERSISTENT_CAMPAIGN_UNAVAILABLE",
            }

    def bind_campaign(self) -> StepResult:
        """离线绑定明确批准的固定公开数据集与累计额度。

        Args:
            无参数；读取配置中的 campaign_limits 和维护确认。

        Returns:
            导入真实历史后的安全累计账本，或准确阻断原因。

        """
        if self.control is None or self.configuration_error:
            return StepResult(
                "BLOCKED", self.configuration_error or "CONFIGURATION_REQUIRED"
            )
        limits = self.config.get("campaign_limits")
        if (
            not isinstance(limits, dict)
            or not {"request_limit", "estimated_token_limit"} <= limits.keys()
        ):
            return StepResult("BLOCKED", "EXPLICIT_CAMPAIGN_LIMITS_REQUIRED")
        try:
            provider_tokens = _p11_limits(limits)
            names = tuple(
                name
                for name in ("jina_connection_id", "aliyun_connection_id")
                if self.config.get(name)
            )
            if not names:
                return StepResult(
                    "BLOCKED", "REQUESTED_CONNECTION_NOT_CONFIGURED"
                )
            connections = tuple(
                self.control.get_connection(str(self.config[name]))
                for name in names
            )
            specs = tuple(self._spec(name) for name in names)
            source = (
                self.control.get_profile(
                    str(self.config["source_profile_revision_id"])
                )
                if self.config.get("source_profile_revision_id")
                else None
            )
            policy = RetrievalPolicy.model_validate(
                {} if source is None else dict(source.retrieval_policy)
            )
            contracts = approved_payload_contracts(
                connections,
                specs,
                policy,
                {
                    item.credential_id: self.control.credential_version(
                        item.credential_id
                    )
                    for item in connections
                },
            )
            campaign = BudgetCampaign(
                campaign_id=str(self.config["campaign_id"]),
                authorization_id=str(self.config["authorization_id"]),
                scope=str(self.config.get("scope", "p11-public-synthetic-v1")),
                request_limit=int(str(limits["request_limit"])),
                estimated_token_limit=int(str(limits["estimated_token_limit"])),
                provider_request_limits=_limit_mapping(
                    limits.get("provider_request_limits", {})
                ),
                provider_token_limits=provider_tokens,
                step_request_limits={
                    "aliyun_document_canary": 1,
                    "aliyun_query_canary": 1,
                },
                **contracts,
            )
            summary = bind_existing_product_campaign(
                Path(str(self.config["data_dir"])),
                campaign,
                maintenance_confirmed=self.config.get("maintenance_confirmed")
                is True,
            )
            return StepResult(
                "PASS",
                "CAMPAIGN_BOUND_WITH_HISTORY",
                {"summary": summary, "http_requests": 0},
            )
        except BudgetBlockedError as error:
            return StepResult(
                "BLOCKED",
                error.reason,
                {"minimum_additional": error.minimum_additional},
            )
        except (RagError, ValueError, OSError, sqlite3.DatabaseError) as error:
            return StepResult(
                "BLOCKED",
                "CAMPAIGN_BINDING_FAILED",
                {"safe_error_type": type(error).__name__},
            )

    def _runtime(self) -> ProductRuntime:
        if self.runtime is None:
            settings = dict(
                cast(dict[str, object], self.config.get("runtime_settings", {}))
            )
            if not settings:
                options = ProductRuntimeSettings.from_environment()
            else:
                settings["data_dir"] = self.config["data_dir"]
                for name in (
                    "data_dir",
                    "frontend_dir",
                    "bootstrap_token_file",
                    "master_key_file",
                    "qdrant_api_key_file",
                    "migrations_dir",
                    "compatibility_manifest",
                ):
                    if settings.get(name) is not None:
                        settings[name] = Path(str(settings[name]))
                options = ProductRuntimeSettings(**settings)  # type: ignore[arg-type]
            if (
                options.data_dir.resolve()
                != Path(str(self.config["data_dir"])).resolve()
            ):
                raise ValueError("Runtime 数据目录与验收身份不一致。")
            self.runtime = build_product_runtime(
                options, circuit_factory=self._circuit, recover_jobs=False
            )
        return self.runtime

    def _providers(self) -> ProviderRuntimeRegistry:
        if self.provider_registry is None:
            settings = cast(
                dict[str, object], self.config.get("runtime_settings", {})
            )
            master_path = settings.get("master_key_file") or os.environ.get(
                "RAG_MASTER_KEY_FILE"
            )
            cipher = (
                None
                if not master_path
                else SecretCipher(load_master_key(Path(str(master_path))))
            )
            database = (
                Path(str(self.config["data_dir"])) / "universal-rag.sqlite3"
            )
            connections = SqliteConnectionFactory(database)
            credentials = CredentialStore(connections, cipher)
            control = ProductControlStore(connections, credentials)
            self.provider_registry = ProviderRuntimeRegistry(
                credentials,
                control,
                budget_ledger_path=Path(str(self.config["ledger_path"])),
            )
        return self.provider_registry

    def _circuit(self) -> ProviderCircuitBreaker:
        circuit = ProviderCircuitBreaker(
            CircuitBreakerPolicy(
                failure_threshold=1,
                open_cooldown_seconds=10,
                recovery_success_threshold=1,
            ),
            clock=lambda: self.clock,
        )
        self.circuits.append(circuit)
        return circuit

    def _canary(self, step: str) -> StepResult:
        operation = (
            "embedding.document"
            if step == "aliyun_document_canary"
            else "embedding.query"
        )
        return self._validate("aliyun_connection_id", operation)

    def _validate(self, key: str, operation: str) -> StepResult:
        if self.control is None:
            return StepResult("BLOCKED", "CONNECTION_METADATA_UNAVAILABLE")
        connection_id = str(self.config[key])
        spec = self._spec(key)
        policy: dict[str, object] = dict(
            spec.document_policy
            if operation.endswith("document")
            else spec.query_policy
        )
        model = "jina-reranker-v3.5" if operation == "reranking" else spec.model
        connection = self.control.get_connection(connection_id)
        expected_policy = (
            canonical_sha256({"model": model, "operation": operation})
            if operation == "reranking"
            else spec.policy_identity(operation)
        )
        for run in self.control.list_validations(connection_id):
            if (
                key == "jina_connection_id"
                and run.operation == operation
                and run.provider_model == model
                and run.request_policy_identity == expected_policy
                and run.validation_mode == "live"
                and run.http_category == "live_200"
                and run.status == "succeeded"
                and validation_is_current(
                    run,
                    connection,
                    self.control.credential_version(connection.credential_id),
                )
            ):
                return StepResult(
                    "PASS",
                    "CURRENT_LIVE_VALIDATION_REUSED",
                    {"validation_id": run.validation_id},
                )
        run = self._providers().validate(
            connection_id,
            operation=operation,
            model=model,
            expected_dimension=None if operation == "reranking" else 1024,
            request_policy={} if operation == "reranking" else policy,
        )
        evidence = run.model_dump(mode="json")
        if run.stage == "budget":
            return StepResult(
                "BLOCKED", run.safe_error_code or "BLOCKED_BUDGET", evidence
            )
        if (
            run.status != "succeeded"
            or run.validation_mode != "live"
            or run.http_category != "live_200"
        ):
            return StepResult(
                "FAIL",
                run.safe_error_code or "LIVE_VALIDATION_FAILED",
                evidence,
            )
        return StepResult("PASS", "LIVE_VALIDATION_PASSED", evidence)

    def _jina(self) -> StepResult:
        evidence: dict[str, object] = {}
        for operation in ("embedding.document", "embedding.query", "reranking"):
            result = self._validate("jina_connection_id", operation)
            evidence[operation] = result.evidence
            if result.status != "PASS":
                return StepResult(result.status, result.reason, evidence)
        return StepResult("PASS", "JINA_OPERATIONS_CURRENT", evidence)

    def _dual_index(self) -> StepResult:
        runtime = self._runtime()
        source = (
            runtime.control.get_profile(
                str(self.config["source_profile_revision_id"])
            )
            if self.config.get("source_profile_revision_id")
            else None
        )
        input_identity = canonical_sha256(
            {
                "campaign": self.state.campaign_id,
                "index": None
                if source is None
                else source.index_semantic_fingerprint,
                "serving": None
                if source is None
                else source.serving_fingerprint,
                "connections": self.identity("dual_index"),
            }
        )
        project = runtime.sdk.create_project(
            "P11 公开合成验收",
            idempotency_key="p11:" + canonical_sha256(self.state.campaign_id),
        )
        kb = runtime.sdk.create_knowledge_base(
            project.project_id,
            "P11 独立验收知识库",
            description="仅包含仓库内公开合成数据。",
            idempotency_key="p11-kb:" + input_identity,
        )
        self.state.resource("project_id", project.project_id)
        self.state.resource("knowledge_base_id", kb.knowledge_base_id)
        profile_id = self.state.resource(
            "profile_revision_id:" + input_identity
        )
        if profile_id is None:
            draft = self._default_profile()
            if source is not None:
                draft = {
                    key: value
                    for key, value in source.model_dump(mode="json").items()
                    if key in RetrievalProfileDraft.model_fields
                }
            draft["knowledge_base_id"] = kb.knowledge_base_id
            profile = runtime.control.create_profile(
                RetrievalProfileDraft.model_validate(draft)
            )
            profile_id = profile.profile_revision_id
            self.state.resource(
                "profile_revision_id:" + input_identity, profile_id
            )
        self.state.resource("profile_revision_id", profile_id)
        profile = runtime.control.get_profile(profile_id)
        if profile.status == "draft":
            runtime.control.activate_profile(
                profile_id,
                confirmed_impact=ImpactKind.NEW_INDEX_REVISION_REQUIRED,
            )
        job_id = self.state.resource("index_job_id:" + input_identity)
        created_here = job_id is None
        if job_id is None:
            job = runtime.sdk.create_document(
                project.project_id,
                kb.knowledge_base_id,
                display_name=DOCUMENT_NAME,
                content=functional_document(),
                media_type=_MEDIA_TYPE,
                idempotency_key="p11-doc:" + input_identity,
            )
            job_id = job.job_id
            self.state.resource("index_job_id:" + input_identity, job_id)
        self.state.resource("index_job_id", job_id)
        deadline = monotonic() + 120
        job = runtime.sdk.get_job(job_id)
        if (
            job.project_id != project.project_id
            or job.knowledge_base_id != kb.knowledge_base_id
            or runtime.p09.store.ingestion_profile_revision_id(job_id)
            != profile_id
        ):
            return StepResult(
                "BLOCKED", "RESUME_JOB_SCOPE_MISMATCH", {"job_id": job_id}
            )
        if not created_here and job.state.value in {"queued", "running"}:
            job = runtime.p09.store.resume_ingestion_job(
                job_id,
                project_id=project.project_id,
                knowledge_base_id=kb.knowledge_base_id,
                idempotency_key="p11-doc:" + input_identity,
            )
            if job.state.value == "queued":
                runtime.jobs.submit(job_id)
            elif job.state.value == "running":
                return StepResult(
                    "BLOCKED", "BLOCKED_INFLIGHT", {"job_id": job_id}
                )
        while (
            job.state.value in {"queued", "running"} and monotonic() < deadline
        ):
            sleep(0.1)
            job = runtime.sdk.get_job(job_id)
        if job.state.value != "succeeded":
            return StepResult(
                "BLOCKED",
                "INDEX_JOB_NOT_SUCCEEDED",
                {"job_id": job_id, "state": job.state.value},
            )
        inspection = runtime.sdk.inspect_revision(
            project.project_id, kb.knowledge_base_id, job.revision_id
        )
        self.state.resource("revision_id", job.revision_id)
        complete = (
            inspection.active
            and inspection.actual_chunk_count > 0
            and inspection.fts_count == inspection.actual_chunk_count
            and inspection.expected_chunk_count == inspection.actual_chunk_count
            and {slot.vector_name for slot in inspection.slot_coverages}
            == {"dense_primary", "dense_standby"}
            and all(
                slot.coverage_ratio == 1.0 and slot.failed_count == 0
                for slot in inspection.slot_coverages
            )
            and inspection.validation_evidence_hash is not None
        )
        return StepResult(
            "PASS" if complete else "FAIL",
            "DUAL_SLOT_VERIFIED" if complete else "DUAL_SLOT_INCOMPLETE",
            inspection.model_dump(mode="json"),
        )

    def _default_profile(self) -> dict[str, object]:
        primary = self._spec("jina_connection_id")
        standby = self._spec("aliyun_connection_id")
        ledger = ProviderBudgetLedger(
            Path(str(self.config["ledger_path"])), read_only=True
        )
        campaign = ledger.campaign(str(self.config["campaign_id"]))
        return {
            "primary_connection_id": self.config["jina_connection_id"],
            "primary_embedding_model": primary.model,
            "primary_dimension": primary.dimension,
            "primary_document_policy": dict(primary.document_policy),
            "primary_query_policy": dict(primary.query_policy),
            "standby_connection_id": self.config["aliyun_connection_id"],
            "standby_embedding_model": standby.model,
            "standby_dimension": standby.dimension,
            "standby_document_policy": dict(standby.document_policy),
            "standby_query_policy": dict(standby.query_policy),
            "reranker_connection_id": self.config["jina_connection_id"],
            "reranker_model": "jina-reranker-v3.5",
            "failover_enabled": True,
            "standby_budget": {
                "requests": campaign.request_limit,
                "tokens": campaign.estimated_token_limit,
            },
        }

    def _local_blocker(self, request: httpx.Request) -> bool:
        payload = json.loads(request.content)
        jina_query = (
            request.url.host == "api.jina.ai"
            and payload.get("task") == "retrieval.query"
        )
        aliyun_query = (
            dict(payload.get("parameters", {})).get("text_type") == "query"
        )
        if jina_query and self.fault_mode is None:
            self.observed_half_open |= any(
                circuit.snapshot(_JINA_KEY).state is CircuitState.HALF_OPEN
                for circuit in self.circuits
            )
        return (jina_query and self.fault_mode in {"jina", "both"}) or (
            aliyun_query and self.fault_mode == "both"
        )

    @contextmanager
    def _fault(self, *, both: bool = False) -> Iterator[None]:
        previous = self.fault_mode
        self.fault_mode = "both" if both else "jina"
        try:
            yield
        finally:
            self.fault_mode = previous

    def _search(self, step: str) -> dict[str, object]:
        runtime = self._runtime()
        project = self.state.resource("project_id")
        kb = self.state.resource("knowledge_base_id")
        if project is None or kb is None:
            raise ValueError("缺少独立验收知识库。")
        # 每阶段使用不同批准查询；新续跑 Runtime 自有缓存为空。
        result = runtime.sdk.search(
            project, kb, QUERIES[step], limit=QUERY_LIMIT
        )
        value = result.model_dump(mode="json")
        value["profile_revision_id"] = self.state.resource(
            "profile_revision_id"
        )
        return value

    def _query(self, step: str) -> StepResult:
        if step == "standby_failover":
            with self._fault():
                result = self._search(step)
            expected = "standby"
        elif step == "recovery":
            # 续跑是新进程；用同一验收 transport 再建立真实失败态。
            with self._fault(both=True):
                unavailable = self._search("standby_unavailable")
            if unavailable["selected_embedding_slot"] is not None:
                return StepResult(
                    "FAIL", "STANDBY_UNAVAILABLE_NOT_OBSERVED", unavailable
                )
            self.clock += 11
            result = self._search(step)
            if not self.observed_half_open:
                return StepResult("FAIL", "HALF_OPEN_NOT_OBSERVED", result)
            if any(
                circuit.snapshot(_JINA_KEY).state is not CircuitState.CLOSED
                for circuit in self.circuits
            ):
                return StepResult("FAIL", "CIRCUIT_NOT_RECOVERED", result)
            expected = "primary"
        else:
            result = self._search(step)
            expected = "primary"
        valid = (
            result["selected_embedding_slot"] == expected
            and result["selected_vector_name"] == "dense_" + expected
            and result["cache_hit"] is False
            and bool(result["evidence"])
            and result["active_index_revision_id"]
            == self.state.resource("revision_id")
        )
        return StepResult(
            "PASS" if valid else "FAIL",
            "ROUTE_VERIFIED" if valid else "ROUTE_CONTRACT_FAILED",
            result,
        )

    def _quality(self) -> StepResult:
        ledger = ProviderBudgetLedger(
            Path(str(self.config["ledger_path"])), read_only=True
        )
        config = {
            **self.config,
            "source_profile_revision_id": self.state.resource(
                "profile_revision_id"
            ),
        }
        return run_pilot(
            self._runtime(),
            config,
            self.state,
            self._pilot_search,
            ledger.summary(str(self.config["campaign_id"])),
        )

    def _pilot_search(
        self, project_id: str, kb_id: str, text: str, lane: str
    ) -> SearchAnswerResult:
        runtime = self._runtime()
        runtime.profiles.invalidate()
        if lane == "standby":
            with self._fault():
                return runtime.sdk.search(
                    project_id, kb_id, text, limit=QUERY_LIMIT
                )
        if lane != "primary":
            raise ValueError("不支持的质量评估路由。")
        return runtime.sdk.search(project_id, kb_id, text, limit=QUERY_LIMIT)

    def close(self) -> None:
        """移除测试 transport 后关闭验收自己的 Runtime。

        Args:
            无参数；释放验收自有资源。

        Returns:
            无返回值。

        """
        if self.runtime is not None:
            self.runtime.close()
        if self.provider_registry is not None:
            self.provider_registry.close()


def _limit_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("Provider 预算必须是映射。")
    return {str(key): int(str(limit)) for key, limit in value.items()}


def _p11_limits(limits: dict[str, object]) -> dict[str, int]:
    requests = int(str(limits["request_limit"]))
    tokens = int(str(limits["estimated_token_limit"]))
    provider_tokens = {
        "jina": _AUTHORIZED_PROVIDER_TOKEN_LIMIT,
        "aliyun": _AUTHORIZED_PROVIDER_TOKEN_LIMIT,
        **_limit_mapping(limits.get("provider_token_limits", {})),
    }
    provider_requests = _limit_mapping(
        limits.get("provider_request_limits", {})
    )
    if (
        not 1 <= requests <= _AUTHORIZED_REQUEST_LIMIT
        or not 1 <= tokens <= _AUTHORIZED_TOKEN_LIMIT
        or set(provider_tokens) != {"jina", "aliyun"}
        or any(
            not 1 <= value <= _AUTHORIZED_PROVIDER_TOKEN_LIMIT
            for value in provider_tokens.values()
        )
        or any(
            not 1 <= value <= _AUTHORIZED_REQUEST_LIMIT
            for value in provider_requests.values()
        )
    ):
        raise BudgetBlockedError("P11_AUTHORIZATION_LIMIT_EXCEEDED")
    return provider_tokens
