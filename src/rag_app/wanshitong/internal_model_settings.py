"""从显式 Primary URL 读取湾事通内网模型配置。"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from rag_app.product.catalog import validate_model
from rag_app.product.openai_compatible import (
    normalize_base_url,
    normalize_rerank_path,
    normalize_rerank_protocol,
)
from rag_app.wanshitong.settings import WanshitongSettings

CredentialSource = Literal["none", "environment", "file"]

_EMBEDDING_BASE_URL = "RAG_WANSHITONG_EMBEDDING_BASE_URL"
_RERANKER_BASE_URL = "RAG_WANSHITONG_RERANKER_BASE_URL"
_LLM_BASE_URL = "RAG_WANSHITONG_LLM_BASE_URL"
_EMBEDDING_MODEL = "RAG_WANSHITONG_EMBEDDING_MODEL"
_EMBEDDING_DIMENSION = "RAG_WANSHITONG_EMBEDDING_DIMENSION"
_RERANKER_MODEL = "RAG_WANSHITONG_RERANKER_MODEL"
_RERANKER_PROTOCOL = "RAG_WANSHITONG_RERANKER_PROTOCOL"
_RERANKER_PATH = "RAG_WANSHITONG_RERANKER_PATH"
_LLM_MODEL = "RAG_WANSHITONG_LLM_MODEL"
_LLM_DISABLE_THINKING_SUPPORTED = (
    "RAG_WANSHITONG_LLM_DISABLE_THINKING_SUPPORTED"
)
_LLM_STRUCTURED_OUTPUT_MODE = "RAG_WANSHITONG_LLM_STRUCTURED_OUTPUT_MODE"
_CREDENTIAL_ENV_SUFFIX = "_CREDENTIAL_ENV"
_API_KEY_FILE_SUFFIX = "_API_KEY_FILE"
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_MAX_EMBEDDING_DIMENSION = 65536
_MAX_SECRET_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class InternalCredentialSettings:
    """描述无鉴权、环境托管或 0600 Secret 文件。"""

    source: CredentialSource = "none"
    environment_name: str | None = None
    secret_file: Path | None = None

    @property
    def safe_identity(self) -> dict[str, str]:
        """返回不含 Secret 的稳定配置身份。"""
        identity: dict[str, str] = {"source": self.source}
        if self.environment_name is not None:
            identity["environment_name"] = self.environment_name
        if self.secret_file is not None:
            identity["secret_file"] = str(self.secret_file)
        return identity

    def database_secret(self) -> str:
        """读取数据库加密 Credential 所需的 Secret。"""
        if self.source == "none":
            return ""
        if self.source != "file" or self.secret_file is None:
            raise ValueError("环境托管 Credential 不应读取 Secret 文件。")
        path = self.secret_file
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                "模型 API Key 文件必须是普通文件且不能是符号链接。"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ValueError("模型 API Key 文件权限必须为 0600 或更严格。")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
        if (
            not value
            or len(value) > _MAX_SECRET_LENGTH
            or "\n" in value
            or "\r" in value
            or "\x00" in value
        ):
            raise ValueError("模型 API Key 文件必须包含单行非空 Secret。")
        return value


@dataclass(frozen=True, slots=True)
class InternalModelSettings:
    """三个既有 OpenAI-compatible Connection 的冻结输入。"""

    embedding_base_url: str
    reranker_base_url: str
    llm_base_url: str
    embedding_model: str = "Qwen3-Embedding-0.6B"
    embedding_dimension: int = 1024
    reranker_model: str = "Qwen3-Reranker-0.6B"
    reranker_protocol: str = "tei"
    reranker_path: str = "/rerank"
    llm_model: str = "Qwen/Qwen3-8B-AWQ"
    llm_disable_thinking_supported: bool = False
    llm_structured_output_mode: Literal[
        "none", "response_format", "structured_outputs", "guided_json"
    ] = "none"
    embedding_credential: InternalCredentialSettings = field(
        default_factory=InternalCredentialSettings
    )
    reranker_credential: InternalCredentialSettings = field(
        default_factory=InternalCredentialSettings
    )
    llm_credential: InternalCredentialSettings = field(
        default_factory=InternalCredentialSettings
    )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> InternalModelSettings:
        """读取三个单 Primary 端点及互斥 Credential 来源。"""
        source = os.environ if environment is None else environment
        mode = WanshitongSettings.from_environment(source)
        if not mode.enabled:
            raise ValueError(
                "内网模型引导仅允许在 RAG_PRODUCT_MODE=wanshitong 下运行。"
            )
        settings = cls(
            embedding_base_url=_base_url(source, _EMBEDDING_BASE_URL),
            reranker_base_url=_base_url(source, _RERANKER_BASE_URL),
            llm_base_url=_base_url(source, _LLM_BASE_URL),
            embedding_model=source.get(
                _EMBEDDING_MODEL, "Qwen3-Embedding-0.6B"
            ),
            embedding_dimension=_positive_int(
                source.get(_EMBEDDING_DIMENSION, "1024"),
                _EMBEDDING_DIMENSION,
            ),
            reranker_model=source.get(
                _RERANKER_MODEL, "Qwen3-Reranker-0.6B"
            ),
            reranker_protocol=normalize_rerank_protocol(
                source.get(_RERANKER_PROTOCOL, "tei")
            ),
            reranker_path=normalize_rerank_path(
                source.get(_RERANKER_PATH, "/rerank"),
                source.get(_RERANKER_PROTOCOL, "tei"),
            ),
            llm_model=source.get(_LLM_MODEL, "Qwen/Qwen3-8B-AWQ"),
            llm_disable_thinking_supported=_boolean(
                source.get(_LLM_DISABLE_THINKING_SUPPORTED, "false"),
                _LLM_DISABLE_THINKING_SUPPORTED,
            ),
            llm_structured_output_mode=_structured_output_mode(
                source.get(_LLM_STRUCTURED_OUTPUT_MODE, "none")
            ),
            embedding_credential=_credential(source, "EMBEDDING"),
            reranker_credential=_credential(source, "RERANKER"),
            llm_credential=_credential(source, "LLM"),
        )
        settings._validate_models()
        if settings.uses_http and not mode.demo_allow_http:
            raise ValueError(
                "内网 HTTP 端点要求显式设置 "
                "RAG_WANSHITONG_DEMO_ALLOW_HTTP=true。"
            )
        return settings

    @property
    def uses_http(self) -> bool:
        """返回任一 Primary Base URL 是否使用明文 HTTP。"""
        return any(
            urlsplit(value).scheme == "http"
            for value in (
                self.embedding_base_url,
                self.reranker_base_url,
                self.llm_base_url,
            )
        )

    def credential_for(
        self, role: Literal["embedding", "reranker", "llm"]
    ) -> InternalCredentialSettings:
        """返回指定模型角色的 Credential 设置。"""
        return {
            "embedding": self.embedding_credential,
            "reranker": self.reranker_credential,
            "llm": self.llm_credential,
        }[role]

    def _validate_models(self) -> None:
        validate_model(
            "openai-compatible",
            self.embedding_model,
            "embedding.document",
        )
        validate_model("openai-compatible", self.reranker_model, "reranking")
        validate_model("openai-compatible", self.llm_model, "generation")
        if self.embedding_dimension > _MAX_EMBEDDING_DIMENSION:
            raise ValueError("Embedding Dimension 不能超过 65536。")


def _base_url(environment: Mapping[str, str], key: str) -> str:
    raw = environment.get(key)
    if raw is None or not raw.strip():
        raise ValueError(f"必须显式配置单个 Primary：{key}。")
    stripped = raw.strip()
    if stripped.startswith("["):
        raise ValueError(
            f"{key} 不接受 Endpoint 数组；请显式选择一个 Primary。"
        )
    return normalize_base_url(stripped)


def _positive_int(value: str, key: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"{key} 必须为正整数。") from None
    if parsed <= 0:
        raise ValueError(f"{key} 必须为正整数。")
    return parsed


def _boolean(value: str, key: str) -> bool:
    normalized = value.strip().casefold()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{key} 必须为 true 或 false。")
    return normalized == "true"


def _structured_output_mode(
    value: str,
) -> Literal["none", "response_format", "structured_outputs", "guided_json"]:
    normalized = value.strip().casefold()
    if normalized not in {
        "none",
        "response_format",
        "structured_outputs",
        "guided_json",
    }:
        raise ValueError(f"{_LLM_STRUCTURED_OUTPUT_MODE} 配置无效。")
    return cast(
        Literal["none", "response_format", "structured_outputs", "guided_json"],
        normalized,
    )


def _credential(
    environment: Mapping[str, str], role: str
) -> InternalCredentialSettings:
    prefix = f"RAG_WANSHITONG_{role}"
    environment_name = environment.get(prefix + _CREDENTIAL_ENV_SUFFIX)
    secret_file = environment.get(prefix + _API_KEY_FILE_SUFFIX)
    environment_name = (
        None if environment_name is None else environment_name.strip()
    )
    secret_file = None if secret_file is None else secret_file.strip()
    if environment_name and secret_file:
        raise ValueError(
            f"{prefix} Credential 环境变量与 Secret 文件只能配置一种。"
        )
    if environment_name:
        if _ENVIRONMENT_NAME.fullmatch(environment_name) is None:
            raise ValueError(f"{prefix}_CREDENTIAL_ENV 不是有效环境变量名。")
        return InternalCredentialSettings(
            source="environment", environment_name=environment_name
        )
    if secret_file:
        return InternalCredentialSettings(
            source="file", secret_file=Path(secret_file)
        )
    return InternalCredentialSettings()


__all__ = ["InternalCredentialSettings", "InternalModelSettings"]
