"""从单个 Primary URL 读取湾事通内网模型配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from rag_app.product.catalog import validate_model
from rag_app.product.openai_compatible import (
    normalize_base_url,
    normalize_rerank_path,
    normalize_rerank_protocol,
)
from rag_app.wanshitong.settings import WanshitongSettings

_EMBEDDING_BASE_URL = "RAG_WANSHITONG_EMBEDDING_BASE_URL"
_RERANKER_BASE_URL = "RAG_WANSHITONG_RERANKER_BASE_URL"
_LLM_BASE_URL = "RAG_WANSHITONG_LLM_BASE_URL"
_EMBEDDING_MODEL = "RAG_WANSHITONG_EMBEDDING_MODEL"
_EMBEDDING_DIMENSION = "RAG_WANSHITONG_EMBEDDING_DIMENSION"
_RERANKER_MODEL = "RAG_WANSHITONG_RERANKER_MODEL"
_RERANKER_PROTOCOL = "RAG_WANSHITONG_RERANKER_PROTOCOL"
_RERANKER_PATH = "RAG_WANSHITONG_RERANKER_PATH"
_LLM_MODEL = "RAG_WANSHITONG_LLM_MODEL"
_MAX_EMBEDDING_DIMENSION = 65536


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

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> InternalModelSettings:
        """读取单个 Primary 端点并拒绝隐式 Endpoint 数组。

        Args:
            environment: 可选环境变量映射；默认读取当前进程环境。

        Returns:
            已规范化并完成本地合同校验的内网模型设置。

        Raises:
            ValueError: 模式、URL、模型、维度或 HTTP 开关无效。

        """
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
            reranker_model=source.get(_RERANKER_MODEL, "Qwen3-Reranker-0.6B"),
            reranker_protocol=normalize_rerank_protocol(
                source.get(_RERANKER_PROTOCOL, "tei")
            ),
            reranker_path=normalize_rerank_path(
                source.get(_RERANKER_PATH, "/rerank"),
                source.get(_RERANKER_PROTOCOL, "tei"),
            ),
            llm_model=source.get(_LLM_MODEL, "Qwen/Qwen3-8B-AWQ"),
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

    def _validate_models(self) -> None:
        """复用 Universal 自定义模型目录校验。"""
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


__all__ = ["InternalModelSettings"]
