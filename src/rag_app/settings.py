"""从环境变量与冻结 JSON 加载运行时配置。"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Self
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_app.contracts import AuthorityLevel, DocumentStatus, PipelineSpec
from rag_app.strict_json import load_json_file
from rag_app.tracing.models import TraceMode

__all__ = [
    "ConfigurationState",
    "RetrievalSettings",
    "RuntimeSettings",
    "SoftRouteSettings",
]

_MIN_SECRET_LENGTH = 32
_SOURCE_ID_LENGTH = 36


class ConfigurationState(StrEnum):
    """需要评测冻结的配置状态。"""

    PROVISIONAL = "provisional"
    FROZEN = "frozen"


class SoftRouteSettings(BaseModel):
    """由冻结集确定的软路由规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str = Field(min_length=1)
    keywords: tuple[str, ...] = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_values(self) -> Self:
        """拒绝空、重复或非稳定来源 ID。"""
        if any(not value for value in (*self.keywords, *self.source_ids)):
            raise ValueError("软路由关键词和来源 ID 不能为空。")
        if len(set(self.keywords)) != len(self.keywords):
            raise ValueError("软路由关键词不能重复。")
        if (
            len(set(self.source_ids)) != len(self.source_ids)
            or any(
                len(source_id) != _SOURCE_ID_LENGTH
                or not source_id.startswith("src_")
                or any(
                    character not in "0123456789abcdef"
                    for character in source_id[4:]
                )
                for source_id in self.source_ids
            )
        ):
            raise ValueError("软路由来源必须是唯一稳定 source ID。")
        return self


class RetrievalSettings(BaseModel):
    """由冻结集确定的检索、上下文与生成边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ConfigurationState
    dense_limit: int = Field(gt=0)
    bm25_limit: int = Field(gt=0)
    rrf_rank_constant: int = Field(gt=0)
    candidate_limit: int = Field(gt=0)
    final_limit: int = Field(gt=0)
    max_final_limit: int = Field(gt=0)
    query_instruction: str = Field(min_length=1)
    max_history_turns: int = Field(gt=0)
    history_token_budget: int = Field(gt=0)
    max_question_tokens: int = Field(gt=0)
    rewrite_output_tokens: int = Field(gt=0)
    max_evidence_tokens: int = Field(gt=0)
    low_ocr_threshold: float = Field(ge=0.0, le=1.0)
    answer_output_tokens: int = Field(gt=0)
    repair_output_tokens: int = Field(gt=0)
    conversation_ttl_seconds: int = Field(gt=0)
    bm25_tokenizer: str = "multilingual"
    bm25_language: str = "none"
    allowed_statuses: tuple[DocumentStatus, ...] = Field(min_length=1)
    allowed_authority_levels: tuple[AuthorityLevel, ...] = Field(
        min_length=1
    )
    soft_route_min_confidence: float = Field(
        default=0.75,
        gt=0.0,
        le=1.0,
    )
    soft_routes: tuple[SoftRouteSettings, ...] = ()

    @model_validator(mode="after")
    def _validate_limits(self) -> Self:
        """校验召回、精排与证据条数的包含关系。"""
        if not (
            self.final_limit
            <= self.max_final_limit
            <= self.candidate_limit
        ):
            raise ValueError(
                "必须满足 final_limit <= max_final_limit "
                "<= candidate_limit。"
            )
        route_ids = tuple(route.route_id for route in self.soft_routes)
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("soft_routes 的 route_id 不能重复。")
        if len(set(self.allowed_statuses)) != len(self.allowed_statuses):
            raise ValueError("allowed_statuses 不能重复。")
        if len(set(self.allowed_authority_levels)) != len(
            self.allowed_authority_levels
        ):
            raise ValueError("allowed_authority_levels 不能重复。")
        return self

    @classmethod
    def load(cls, path: Path) -> RetrievalSettings:
        """从 UTF-8 JSON 文件加载严格配置。

        Args:
            path: 本地冻结配置文件。

        Returns:
            已完成 schema 校验的配置。

        """
        return cls.model_validate(
            load_json_file(path, label="retrieval")
        )

    def serving_fingerprint(self, pipeline: PipelineSpec) -> str:
        """计算查询服务配置的规范化指纹。

        Args:
            pipeline: 提供索引指纹和模型版本的 pipeline 契约。

        Returns:
            带算法前缀的 SHA256 服务指纹。

        """
        routes = [
            {
                "route_id": route.route_id,
                "keywords": sorted(route.keywords),
                "source_ids": sorted(route.source_ids),
            }
            for route in sorted(
                self.soft_routes,
                key=lambda route: route.route_id,
            )
        ]
        payload = {
            "index_fingerprint": pipeline.fingerprint(),
            "query_instruction": self.query_instruction,
            "rewrite": {
                "max_history_turns": self.max_history_turns,
                "history_token_budget": self.history_token_budget,
                "max_question_tokens": self.max_question_tokens,
                "output_tokens": self.rewrite_output_tokens,
                "conversation_ttl_seconds": (
                    self.conversation_ttl_seconds
                ),
            },
            "retrieval": {
                "dense_limit": self.dense_limit,
                "bm25_limit": self.bm25_limit,
                "rrf_rank_constant": self.rrf_rank_constant,
                "candidate_limit": self.candidate_limit,
            },
            "metadata_filter": {
                "allowed_statuses": sorted(self.allowed_statuses),
                "allowed_authority_levels": sorted(
                    self.allowed_authority_levels
                ),
            },
            "soft_routing": {
                "minimum_confidence": self.soft_route_min_confidence,
                "routes": routes,
            },
            "reranker": {
                "model": pipeline.reranker_model,
                "revision": pipeline.reranker_revision,
                "candidate_limit": self.candidate_limit,
                "final_limit": self.final_limit,
                "max_final_limit": self.max_final_limit,
            },
            "neighbors": {"max_items": self.max_final_limit},
            "evidence": {
                "max_tokens": self.max_evidence_tokens,
                "max_items": self.max_final_limit,
                "low_ocr_threshold": self.low_ocr_threshold,
            },
            "output": {
                "answer_tokens": self.answer_output_tokens,
                "repair_tokens": self.repair_output_tokens,
            },
            "llm": {
                "model": pipeline.llm_model,
                "revisions": sorted(pipeline.llm_revisions),
                "prompt_revision": pipeline.prompt_revision,
                "tokenizer_sha256": pipeline.llm_tokenizer_sha256,
            },
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            "sha256:"
            f"{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        )


class RuntimeSettings(BaseSettings):
    """只从环境接收端点、路径和密钥的运行时设置。"""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        extra="forbid",
        frozen=True,
    )

    query_token: SecretStr = Field(min_length=_MIN_SECRET_LENGTH)
    admin_token: SecretStr = Field(min_length=_MIN_SECRET_LENGTH)
    qdrant_api_key: SecretStr = Field(min_length=_MIN_SECRET_LENGTH)
    qdrant_url: str
    qdrant_alias: str = Field(min_length=1, max_length=128)
    state_database: Path
    manifest_database: Path
    trace_database: Path = Path("/state/traces.sqlite3")
    trace_mode: TraceMode = TraceMode.SAFE
    release_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    pipeline_path: Path
    retrieval_path: Path
    corpus_policy_path: Path = Path(
        "/app/deployment/config/corpus-policy.json"
    )
    frontend_dir: Path
    llm_tokenizer_path: Path
    embedding_tokenizer_path: Path = Path(
        "/app/deployment/assets/tokenizers/embedding/tokenizer.json"
    )
    input_root: Path = Path("/data/docs")
    index_state_dir: Path = Path("/state/indexes")
    embedding_endpoints: str
    reranker_endpoints: str
    llm_endpoints: str
    ocr_endpoints: str = '["http://rag-ocr:8090"]'
    embedding_model: str = Field(min_length=1)
    reranker_model: str = Field(min_length=1)
    llm_model: str = Field(min_length=1)
    embedding_api_token: SecretStr | None = None
    reranker_api_token: SecretStr | None = None
    llm_api_token: SecretStr | None = None
    ocr_api_token: SecretStr | None = None
    http_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    reranker_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    ocr_timeout_seconds: float = Field(default=35.0, gt=0)
    ocr_max_input_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
    )
    max_attempts: int = Field(default=4, ge=1, le=8)
    failure_threshold: int = Field(default=2, ge=1, le=8)
    cooldown_seconds: float = Field(default=30.0, gt=0)
    max_embedding_concurrency: int = Field(default=4, ge=1, le=32)
    max_reranker_concurrency: int = Field(default=4, ge=1, le=32)
    max_llm_concurrency: int = Field(default=4, ge=1, le=32)
    max_ocr_concurrency: int = Field(default=1, ge=1, le=1)
    embedding_max_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_max_batch_chars: int = Field(default=131_072, ge=1)
    llm_max_context_tokens: int = Field(default=8192, ge=1)
    host: str = "0.0.0.0"  # noqa: S104
    port: int = Field(default=8088, ge=1, le=65535)

    @model_validator(mode="after")
    def _validate_runtime(self) -> Self:
        """拒绝复用令牌、无效 URL 与空端点集合。"""
        if self.query_token.get_secret_value() == (
            self.admin_token.get_secret_value()
        ):
            raise ValueError("查询令牌与管理令牌必须不同。")
        _validate_http_url(self.qdrant_url)
        self.embedding_endpoint_urls()
        self.reranker_endpoint_urls()
        self.llm_endpoint_urls()
        self.ocr_endpoint_urls()
        return self

    def embedding_endpoint_urls(self) -> tuple[str, ...]:
        """返回规范化 embedding 端点。

        Args:
            无参数；解析当前 embedding 配置。

        Returns:
            非空且去除尾斜杠的端点元组。

        """
        return _parse_endpoint_list(self.embedding_endpoints)

    def reranker_endpoint_urls(self) -> tuple[str, ...]:
        """返回规范化 reranker 端点。

        Args:
            无参数；解析当前 reranker 配置。

        Returns:
            非空且去除尾斜杠的端点元组。

        """
        return _parse_endpoint_list(self.reranker_endpoints)

    def llm_endpoint_urls(self) -> tuple[str, ...]:
        """返回规范化 LLM 端点。

        Args:
            无参数；解析当前 LLM 配置。

        Returns:
            非空且去除尾斜杠的端点元组。

        """
        return _parse_endpoint_list(self.llm_endpoints)

    def ocr_endpoint_urls(self) -> tuple[str, ...]:
        """返回规范化 OCR 端点。

        Args:
            无参数；解析当前 OCR 配置。

        Returns:
            非空且去除尾斜杠的端点元组。

        """
        return _parse_endpoint_list(self.ocr_endpoints)


def _parse_endpoint_list(raw_value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError("端点配置必须是 JSON 字符串数组。") from error
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) for item in decoded)
    ):
        raise ValueError("端点配置必须是非空 JSON 字符串数组。")
    normalized = tuple(item.rstrip("/") for item in decoded)
    if len(set(normalized)) != len(normalized):
        raise ValueError("端点配置不能重复。")
    for endpoint in normalized:
        _validate_http_url(endpoint)
    return normalized


def _validate_http_url(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "服务 URL 必须是无凭据、query 和 fragment 的 HTTP(S)。"
        )
