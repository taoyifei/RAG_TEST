"""以只读非秘密元数据诊断既有连接，不创建 Runtime 或触碰凭据材料。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from rag_app.adapters.providers.aliyun_endpoint import (
    AliyunEndpointConfig,
    AliyunEndpointError,
    resolve_endpoint,
)
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.product.models import ProviderConnection
from rag_app.product.resolved_profile import (
    ResolvedEmbeddingSpec,
    resolve_embedding,
    resolve_retrieval_policy,
)

_MAX_ISSUES = 40
_DIMENSION = 1024
_CONNECTIONS = {
    "jina_connection_id": ("jina", "primary", "jina-embeddings-v5-text-small"),
    "aliyun_connection_id": (
        "aliyun-model-studio",
        "standby",
        "qwen3.7-text-embedding",
    ),
}
_CONNECTION_QUERY = (
    "SELECT connection_id,display_name,provider_type,credential_id,"
    "endpoint_profile,"
    "configuration_version,enabled,status,created_at,updated_at,"
    "json_extract(config_json,'$.endpoint_mode') AS endpoint_mode,"
    "json_extract(config_json,'$.workspace_id') AS workspace_id,"
    "json_extract(config_json,'$.api_host') AS api_host,"
    "json_extract(config_json,'$.region') AS region,"
    "json_extract(config_json,'$.request_budget') AS request_budget,"
    "json_extract(config_json,'$.token_budget') AS token_budget "
    "FROM provider_connections WHERE connection_id=?"
)
_METADATA_FIELDS = (
    "endpoint_mode",
    "workspace_id",
    "api_host",
    "region",
    "request_budget",
    "token_budget",
)
_PROFILE_QUERY = (
    "SELECT primary_connection_id,primary_embedding_model,primary_dimension,"
    "primary_document_policy_json,primary_query_policy_json,"
    "standby_connection_id,standby_embedding_model,standby_dimension,"
    "standby_document_policy_json,standby_query_policy_json,"
    "primary_resolved_json,standby_resolved_json,retrieval_policy_json,"
    "evidence_policy_json FROM retrieval_profile_revisions "
    "WHERE profile_revision_id=?"
)


@dataclass
class _Diagnosis:
    issues: list[dict[str, str]] = field(default_factory=list)
    checks: dict[str, object] = field(default_factory=dict)
    connections: dict[str, dict[str, object]] = field(default_factory=dict)
    safe_error_type: str | None = None
    failed_scopes: set[str] = field(default_factory=set)

    def issue(
        self,
        field_name: str,
        code: str,
        message: str,
        action: str,
        *,
        scope: str = "connection_configuration",
    ) -> None:
        """追加有界字段诊断，禁止混入原始配置值。

        Args:
            field_name: 可定位的非秘密字段路径。
            code: 稳定错误类别。
            message: 安全中文说明。
            action: 能解除问题的具体操作。
            scope: 被阻断的独立检查范围。

        Returns:
            无返回值；达到上限后不再追加。

        """
        if len(self.issues) < _MAX_ISSUES:
            self.issues.append(
                {
                    "field": field_name,
                    "reason_code": code,
                    "message": message,
                    "next_action": action,
                    "blocking_scope": scope,
                }
            )

    def report(self) -> dict[str, object]:
        """聚合独立诊断状态，检查通过不产生网络授权。

        Args:
            无参数；使用当前诊断收集的检查与问题。

        Returns:
            不含配置原文的安全机器报告。

        """
        scopes = {item["blocking_scope"] for item in self.issues}
        endpoint = "BLOCKED" if "endpoint_contract" in scopes else "PASS"
        configuration = (
            "BLOCKED"
            if scopes & {"endpoint_contract", "connection_configuration"}
            else "PASS"
        )
        binding = "BLOCKED" if "campaign_binding" in scopes else "PASS"
        if "endpoint_contract" in self.failed_scopes:
            endpoint = "FAIL"
            configuration = "FAIL"
        if "campaign_binding" in self.failed_scopes:
            binding = "FAIL"
        return {
            "status": "FAIL"
            if self.safe_error_type
            else ("BLOCKED" if self.issues else "PASS"),
            "reason": "DIAGNOSTIC_EXECUTION_FAILED"
            if self.safe_error_type
            else self.issues[0]["reason_code"]
            if self.issues
            else "LOCAL_CONFIGURATION_VALID",
            "endpoint_contract": endpoint,
            "connection_configuration": configuration,
            "campaign_binding": binding,
            "live_allowed": False,
            "checks": self.checks,
            "connections": self.connections,
            "issues": self.issues,
            "http_requests": 0,
            "secret_decryption": False,
            "migrations": False,
            **(
                {"safe_error_type": self.safe_error_type}
                if self.safe_error_type
                else {}
            ),
        }


def diagnose_configuration(config: Mapping[str, object]) -> dict[str, object]:
    """分列端点、连接和预算首绑；成功诊断不授予任何出站权限。

    Args:
        config: 既有 release 入口使用的非秘密配置。

    Returns:
        有界、安全的检查结果。SQL 或未知实现错误保持 FAIL，并只记录异常类型。

    """
    result = _Diagnosis()
    for scope, check in (
        ("endpoint_contract", _diagnose_product),
        ("campaign_binding", _diagnose_campaign),
    ):
        try:
            check(config, result)
        except Exception as error:
            # 诊断边界仅暴露异常类型，失败不能污染另一个独立诊断范围。
            result.safe_error_type = type(error).__name__
            result.failed_scopes.add(scope)
            result.issue(
                "diagnostic",
                "DIAGNOSTIC_EXECUTION_FAILED",
                "本地诊断执行失败，尚不能确认配置原因。",
                "维护者检查对应数据库结构或代码；不要重新输入密钥。",
                scope=scope,
            )
    return result.report()


def _diagnose_product(config: Mapping[str, object], result: _Diagnosis) -> None:
    if not config.get("data_dir"):
        result.issue(
            "data_dir",
            "MISSING_CONFIG:data_dir",
            "未指定产品数据目录。",
            "在既有 release 配置中指定原产品数据目录。",
            scope="endpoint_contract",
        )
        return
    database = Path(str(config["data_dir"])).resolve() / "universal-rag.sqlite3"
    if not database.is_file():
        result.issue(
            "data_dir",
            "PRODUCT_DATABASE_NOT_FOUND",
            "原产品数据库不存在。",
            "检查既有数据卷挂载和 release 配置，不新建替代数据库。",
            scope="endpoint_contract",
        )
        return
    with closing(_read_only(database)) as connection:
        profile = _read_profile(connection, config, result)
        names = [name for name in _CONNECTIONS if config.get(name)]
        if not names:
            result.issue(
                "connection",
                "REQUESTED_CONNECTION_NOT_CONFIGURED",
                "未指定要检查的原连接。",
                "在 release 配置引用已有连接。",
                scope="endpoint_contract",
            )
        for name in names:
            _diagnose_connection(connection, name, config, profile, result)


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _read_profile(
    connection: sqlite3.Connection,
    config: Mapping[str, object],
    result: _Diagnosis,
) -> sqlite3.Row | None:
    source = config.get("source_profile_revision_id")
    result.checks["source_profile_present_or_not_required"] = True
    result.checks["profile_policy_valid"] = True
    if not source:
        return None
    profile = connection.execute(
        _PROFILE_QUERY,
        (str(source),),
    ).fetchone()
    if profile is None:
        result.checks["source_profile_present_or_not_required"] = False
        result.checks["profile_policy_valid"] = None
        result.issue(
            "source_profile_revision_id",
            "SOURCE_PROFILE_NOT_FOUND",
            "指定的源检索方案不存在。",
            "选择实际存在且引用原连接的方案。",
        )
        return None
    try:
        resolve_retrieval_policy(
            _object(profile["retrieval_policy_json"]),
            _object(profile["evidence_policy_json"]),
        )
    except ValueError as error:
        result.checks["profile_policy_valid"] = False
        _policy_issue(result, "source_profile_revision_id", error)
    return cast(sqlite3.Row, profile)


def _object(value: str | None) -> dict[str, Any]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("策略必须是 JSON 对象。")
    return parsed


def _diagnose_connection(
    database: sqlite3.Connection,
    name: str,
    config: Mapping[str, object],
    profile: sqlite3.Row | None,
    result: _Diagnosis,
) -> None:
    checks: dict[str, object] = {
        "connection_exists": False,
        "connection_enabled": None,
        "endpoint_mode": None,
        "workspace_shape_valid": None,
        "api_host_present": None,
        "api_host_shape_valid": None,
        "region_supported": None,
        "credential_metadata_present": None,
        "credential_version_valid": None,
        "source_profile_present_or_not_required": result.checks[
            "source_profile_present_or_not_required"
        ],
        "profile_connection_match": None,
        "profile_policy_valid": None,
    }
    result.connections[name] = checks
    before = len(result.issues)
    row = database.execute(
        _CONNECTION_QUERY,
        (str(config[name]),),
    ).fetchone()
    if row is None:
        result.issue(
            name,
            "CONNECTION_NOT_FOUND",
            "指定的原连接不存在。",
            "核对 release 配置引用的原连接。",
            scope="endpoint_contract",
        )
        checks["status"] = "BLOCKED"
        return
    checks["connection_exists"] = True
    checks["connection_enabled"] = bool(row["enabled"])
    if not row["enabled"]:
        result.issue(
            name + ".enabled",
            "CONNECTION_DISABLED",
            "原连接已停用。",
            "在原连接编辑页面启用并保存。",
        )
    metadata = {key: row[key] for key in _METADATA_FIELDS}
    if name == "aliyun_connection_id":
        _diagnose_endpoint(metadata, name, checks, result)
    _diagnose_credential(database, row, name, checks, result)
    _diagnose_policy(row, name, profile, checks, result)
    checks["status"] = (
        "BLOCKED"
        if len(result.issues) > before
        or any(
            checks[key] is False
            for key in (
                "source_profile_present_or_not_required",
                "profile_policy_valid",
            )
        )
        else "PASS"
    )


def _diagnose_endpoint(
    metadata: dict[str, Any],
    name: str,
    checks: dict[str, object],
    result: _Diagnosis,
) -> None:
    mode = metadata.get("endpoint_mode")
    valid_mode = mode in ("workspace_host", "beijing_dashscope")
    workspace = metadata.get("workspace_id")
    workspace_valid = (
        isinstance(workspace, str)
        and re.fullmatch(
            r"[A-Za-z0-9_-]{1,200}", workspace.strip(" "), flags=re.ASCII
        )
        is not None
    )
    checks.update(
        {
            "endpoint_mode": mode if valid_mode else None,
            "workspace_shape_valid": workspace_valid,
            "api_host_present": bool(metadata.get("api_host")),
            "region_supported": metadata.get("region") == "cn-beijing",
        }
    )
    if not valid_mode:
        result.issue(
            name + ".endpoint_mode",
            "ALIYUN_ENDPOINT_MODE_REQUIRED"
            if not mode
            else "ALIYUN_ENDPOINT_MODE_INVALID",
            "端点模式缺失或不受支持。",
            "编辑原连接，明确选择北京业务空间或北京 DashScope 并保存。",
            scope="endpoint_contract",
        )
    if not workspace_valid:
        result.issue(
            name + ".workspace_id",
            "ALIYUN_WORKSPACE_FORMAT_INVALID",
            "工作空间标识未满足有界 ASCII 形状。",
            "从当前业务空间复制真实 Workspace ID，保留其原有前缀。",
            scope="endpoint_contract",
        )
    if not checks["region_supported"]:
        result.issue(
            name + ".region",
            "ALIYUN_REGION_MISMATCH",
            "连接地域缺失或不是当前支持的北京地域。",
            "核对原连接和控制台地域，在原连接保存北京地域。",
            scope="endpoint_contract",
        )
    if valid_mode:
        _diagnose_host(metadata, name, checks, result)
    elif not metadata.get("api_host"):
        checks["api_host_shape_valid"] = False
        result.issue(
            name + ".api_host",
            "ALIYUN_API_HOST_REQUIRED",
            "未保存业务空间 API Host，当前端点模式也尚未明确。",
            "编辑原连接：若选业务空间模式，从北京控制台 API Key 弹窗或"
            "业务空间管理 API Host 列复制；保存但不立即测试，保留原凭据。",
            scope="endpoint_contract",
        )


def _diagnose_host(
    metadata: dict[str, Any],
    name: str,
    checks: dict[str, object],
    result: _Diagnosis,
) -> None:
    # Host 与用户 Workspace 的形状分别检查，不以任一值推导另一个值。
    try:
        resolve_endpoint(
            AliyunEndpointConfig.model_validate(
                {
                    "endpoint_mode": metadata["endpoint_mode"],
                    "workspace_id": "diagnostic-shape-only",
                    "api_host": metadata.get("api_host"),
                }
            )
        )
    except (AliyunEndpointError, ValidationError) as error:
        checks["api_host_shape_valid"] = False
        code = (
            error.reason_code
            if isinstance(error, AliyunEndpointError)
            else "ALIYUN_API_HOST_FORMAT_INVALID"
        )
        result.issue(
            name + ".api_host",
            code,
            "业务空间 API Host 缺失或不满足所选模式的可信主机规则。",
            "编辑原连接：从百炼北京控制台 API Key 弹窗或业务空间管理的 "
            "API Host 列复制，保存但不立即测试；保留原 Credential。",
            scope="endpoint_contract",
        )
    else:
        checks["api_host_shape_valid"] = True


def _diagnose_credential(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    name: str,
    checks: dict[str, object],
    result: _Diagnosis,
) -> None:
    credential = database.execute(
        "SELECT provider_type,key_version,status FROM provider_credentials "
        "WHERE credential_id=?",
        (row["credential_id"],),
    ).fetchone()
    present = credential is not None
    checks["credential_metadata_present"] = present
    checks["credential_version_valid"] = (
        present
        and type(credential["key_version"]) is int
        and credential["key_version"] > 0
    )
    if not present:
        result.issue(
            name + ".credential_id",
            "CREDENTIAL_METADATA_MISSING",
            "原连接引用的凭据元数据不存在。",
            "维护者核对原凭据引用或备份；诊断不要求重新输入 Key。",
        )
    elif not checks["credential_version_valid"]:
        result.issue(
            name + ".credential_version",
            "CREDENTIAL_VERSION_INVALID",
            "凭据元数据版本无效。",
            "维护者检查凭据元数据完整性。",
        )
    elif credential["provider_type"] != row["provider_type"]:
        result.issue(
            name + ".credential_id",
            "CREDENTIAL_PROVIDER_MISMATCH",
            "凭据元数据与连接服务商不匹配。",
            "维护者核对原凭据引用。",
        )
    elif credential["status"] != "configured":
        result.issue(
            name + ".credential_id",
            "CREDENTIAL_METADATA_NOT_CONFIGURED",
            "凭据元数据尚未配置就绪。",
            "维护者检查凭据托管状态。",
        )


def _diagnose_policy(
    row: sqlite3.Row,
    name: str,
    profile: sqlite3.Row | None,
    checks: dict[str, object],
    result: _Diagnosis,
) -> None:
    metadata = {key: row[key] for key in _METADATA_FIELDS}
    expected_provider, slot, model = _CONNECTIONS[name]
    if row["provider_type"] != expected_provider:
        result.issue(
            name + ".provider_type",
            "CONNECTION_PROVIDER_MISMATCH",
            "连接服务商与所选操作不匹配。",
            "引用对应服务商的原连接。",
        )
        return
    if profile is None and not checks["source_profile_present_or_not_required"]:
        return
    match = (
        profile is None
        or profile[slot + "_connection_id"] == row["connection_id"]
    )
    checks["profile_connection_match"] = match
    if not match:
        result.issue(
            name + ".profile",
            "PROFILE_CONNECTION_MISMATCH",
            "源方案对应槽未引用指定的原连接。",
            "选择引用此原连接的检索方案，或修正 release 的连接引用。",
        )
        return
    try:
        values = dict(row)
        # Jina 没有百炼模式字段；不替百炼补一个未经用户选择的模式。
        if name == "jina_connection_id":
            values["endpoint_mode"] = metadata.get("endpoint_mode") or (
                "workspace_host"
            )
        elif metadata.get("endpoint_mode") is None:
            return
        connection = ProviderConnection.model_validate(values)
        spec = _profile_spec(connection, slot, model, profile)
        if spec.model != model or spec.dimension != _DIMENSION:
            raise ValueError("P11 模型和维度不匹配。")
        checks["profile_policy_valid"] = result.checks["profile_policy_valid"]
    except ValueError as error:
        checks["profile_policy_valid"] = False
        _policy_issue(result, name + ".profile", error)


def _profile_spec(
    connection: ProviderConnection,
    slot: str,
    model: str,
    profile: sqlite3.Row | None,
) -> ResolvedEmbeddingSpec:
    if profile is None:
        return resolve_embedding(connection, model, _DIMENSION, {}, {})
    resolved = _object(profile[slot + "_resolved_json"])
    if resolved:
        spec = ResolvedEmbeddingSpec.model_validate(resolved)
        if spec.connection_id != connection.connection_id:
            raise ValueError("解析策略连接不匹配。")
        # 持久解析对象也须通过当前实际适配器策略校验。
        return resolve_embedding(
            connection,
            spec.model,
            spec.dimension,
            dict(spec.document_policy),
            dict(spec.query_policy),
        )
    return resolve_embedding(
        connection,
        profile[slot + "_embedding_model"],
        profile[slot + "_dimension"],
        _object(profile[slot + "_document_policy_json"]),
        _object(profile[slot + "_query_policy_json"]),
    )


def _policy_issue(result: _Diagnosis, name: str, error: ValueError) -> None:
    # Pydantic 仅输出有界字段路径和错误类别，绝不复制 input/ctx/message 原文。
    suffix = ""
    if isinstance(error, ValidationError):
        categories = [
            ".".join(str(part) for part in item["loc"]) + ":" + item["type"]
            for item in error.errors(
                include_url=False, include_context=False, include_input=False
            )[:5]
        ]
        suffix = "（" + ";".join(categories)[:400] + "）"
    result.issue(
        name,
        "PROFILE_POLICY_INVALID",
        "连接或方案策略不符合当前合同。" + suffix,
        "维护者检查原连接预算、模型、维度与文档/查询策略，保留原凭据。",
    )


def _diagnose_campaign(
    config: Mapping[str, object],
    result: _Diagnosis,
) -> None:
    result.checks.update(
        {
            "ledger_path_valid": False,
            "campaign_exists": False,
            "campaign_authorization_matches": None,
            "campaign_binding_status": "BLOCKED",
        }
    )
    data_dir = config.get("data_dir")
    expected = Path(str(data_dir)).resolve() / "provider-budget.sqlite3"
    actual = Path(str(config.get("ledger_path") or expected)).resolve()
    valid = bool(data_dir) and actual == expected
    result.checks["ledger_path_valid"] = valid
    if not valid:
        result.issue(
            "ledger_path",
            "PRODUCT_LEDGER_PATH_MISMATCH",
            "账本路径不在原产品数据目录。",
            "引用原产品同目录持久预算账本。",
            scope="campaign_binding",
        )
        return
    if not actual.is_file() or not config.get("campaign_id"):
        _binding_required(result)
        return
    with closing(_read_only(actual)) as connection:
        row = connection.execute(
            "SELECT json_extract(configuration,'$.authorization_id') AS "
            "authorization_id,json_extract(configuration,'$.scope') AS scope,"
            "status FROM provider_budget_campaigns WHERE campaign_id=?",
            (str(config["campaign_id"]),),
        ).fetchone()
        result.checks["campaign_exists"] = row is not None
        if row is None:
            _binding_required(result)
            return
        matched = row["authorization_id"] == config.get(
            "authorization_id"
        ) and row["scope"] == config.get("scope", "p11-public-synthetic-v1")
        result.checks["campaign_authorization_matches"] = matched
        if not matched or row["status"] != "ACTIVE":
            result.issue(
                "campaign",
                "CAMPAIGN_AUTHORIZATION_MISMATCH",
                "持久预算授权身份、范围或状态不匹配。",
                "核对既有授权及累计账本，不重新创建额度。",
                scope="campaign_binding",
            )
        active = connection.execute(
            "SELECT campaign_id FROM provider_budget_active_campaign "
            "WHERE singleton=1"
        ).fetchone()
        if active is None or active["campaign_id"] != config["campaign_id"]:
            _binding_required(result)
        elif matched and row["status"] == "ACTIVE":
            _diagnose_current_authorization(actual, config, result)


def _diagnose_current_authorization(
    path: Path, config: Mapping[str, object], result: _Diagnosis
) -> None:
    # read_only 构造不建表；复用真实授权链校验，不能靠原始身份匹配放过过期修订。
    ledger = ProviderBudgetLedger(path, read_only=True)
    try:
        active = ledger.active_campaign()
        if active is None or active.campaign_id != config["campaign_id"]:
            _binding_required(result)
            return
    except BudgetBlockedError as error:
        result.checks["campaign_revision_valid"] = False
        result.issue(
            "campaign.authorization_revision",
            error.reason,
            "当前追加授权已过期或未通过持久审批链校验。",
            "维护者检查原 campaign 的审批修订；有效人工批准前保持阻断。",
            scope="campaign_binding",
        )
        return
    result.checks["campaign_revision_valid"] = True
    result.checks["campaign_binding_status"] = "PASS"


def _binding_required(result: _Diagnosis) -> None:
    result.issue(
        "campaign",
        "CAMPAIGN_BINDING_REQUIRED",
        "原产品尚未绑定指定的持久预算授权。",
        "使用既有离线维护首绑入口导入全部历史消费后绑定；不发起测试。",
        scope="campaign_binding",
    )
