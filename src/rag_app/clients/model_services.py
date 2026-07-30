"""TEI embedding 与内部 reranker 的严格 schema 客户端。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import partial

from rag_app.clients.resilience import (
    HttpJsonResponse,
    ResilientHttpPool,
)

__all__ = [
    "EmbeddingClientConfig",
    "EmbeddingResult",
    "ExternalCallAudit",
    "RerankItem",
    "RerankResult",
    "RerankerClient",
    "TeiEmbeddingClient",
]


@dataclass(frozen=True, slots=True)
class EmbeddingClientConfig:
    """Embedding 服务的冻结响应与批次契约。"""

    model: str
    dimension: int
    max_batch_size: int
    max_batch_chars: int

    def __post_init__(self) -> None:
        """拒绝空模型或非正数预算。"""
        if (
            not self.model.strip()
            or min(
                self.dimension,
                self.max_batch_size,
                self.max_batch_chars,
            )
            <= 0
        ):
            raise ValueError("embedding 模型、维度和批次预算必须有效。")


@dataclass(frozen=True, slots=True)
class ExternalCallAudit:
    """不含请求体与响应正文的外部调用记录。"""

    endpoint: str
    retry_count: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """顺序与输入一致的 embedding 批次结果。"""

    vectors: tuple[tuple[float, ...], ...]
    calls: tuple[ExternalCallAudit, ...]


@dataclass(frozen=True, slots=True)
class RerankItem:
    """一条原始候选索引及模型分数。"""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    """完整且按原始索引排序的 rerank 结果。"""

    items: tuple[RerankItem, ...]
    call: ExternalCallAudit


class TeiEmbeddingClient:
    """调用 TEI OpenAI 兼容 embedding schema。"""

    def __init__(
        self,
        pool: ResilientHttpPool,
        *,
        config: EmbeddingClientConfig,
        api_token: str | None,
    ) -> None:
        """冻结向量维度与请求预算。

        Args:
            pool: embedding 专属韧性端点池。
            config: 冻结模型、维度与批次预算。
            api_token: 可选内部 Bearer token。

        """
        self._pool = pool
        self._model = config.model
        self._dimension = config.dimension
        self._max_batch_size = config.max_batch_size
        self._max_batch_chars = config.max_batch_chars
        self._headers = _authorization_headers(api_token)

    def embed(
        self,
        texts: tuple[str, ...],
        *,
        instruction: str,
    ) -> EmbeddingResult:
        """批量生成向量并严格校验顺序、数量、维度和数值。

        Args:
            texts: 非空原始文本序列。
            instruction: Qwen3 embedding 检索任务指令。

        Returns:
            与输入顺序一致的向量和非敏感调用审计。

        Raises:
            ValueError: 输入或响应 schema 不满足冻结契约。

        """
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("embedding 输入必须是非空文本序列。")
        inputs = tuple(_apply_instruction(text, instruction) for text in texts)
        vectors: list[tuple[float, ...]] = []
        audits: list[ExternalCallAudit] = []
        for batch in _batch_inputs(
            inputs,
            max_batch_size=self._max_batch_size,
            max_batch_chars=self._max_batch_chars,
        ):
            response = self._pool.request_json(
                "POST",
                "/v1/embeddings",
                payload={
                    "model": self._model,
                    "input": list(batch),
                    "truncate": False,
                    "encoding_format": "float",
                },
                headers=self._headers,
                validator=partial(
                    _validate_embedding_payload,
                    expected_model=self._model,
                    expected_count=len(batch),
                    dimension=self._dimension,
                ),
            )
            vectors.extend(
                _parse_embedding_payload(
                    response.payload,
                    expected_model=self._model,
                    expected_count=len(batch),
                    dimension=self._dimension,
                )
            )
            audits.append(_audit(response))
        if len(vectors) != len(texts):
            raise ValueError("embedding 响应总数与输入不一致。")
        return EmbeddingResult(
            vectors=tuple(vectors),
            calls=tuple(audits),
        )


class RerankerClient:
    """调用内部 Qwen3-Reranker 严格 API。"""

    def __init__(
        self,
        pool: ResilientHttpPool,
        *,
        api_token: str | None,
    ) -> None:
        """保存 reranker 专属端点池与鉴权头。

        Args:
            pool: reranker 专属韧性端点池。
            api_token: 可选内部 Bearer token。

        """
        self._pool = pool
        self._headers = _authorization_headers(api_token)

    def rerank(
        self,
        query: str,
        documents: tuple[str, ...],
    ) -> RerankResult:
        """对完整候选集评分，禁止服务端静默截断。

        Args:
            query: 非空查询。
            documents: 非空候选文本。

        Returns:
            按原始索引排序且数量完整的分数。

        Raises:
            ValueError: 输入或响应 schema 无效。

        """
        if not query.strip() or not documents:
            raise ValueError("rerank 查询与候选均不能为空。")
        if any(not document.strip() for document in documents):
            raise ValueError("rerank 候选不能含空文本。")
        response = self._pool.request_json(
            "POST",
            "/rerank",
            payload={
                "query": query,
                "texts": list(documents),
                "truncate": False,
            },
            headers=self._headers,
            validator=partial(
                _validate_rerank_payload,
                expected_count=len(documents),
            ),
        )
        return RerankResult(
            items=_parse_rerank_payload(
                response.payload,
                expected_count=len(documents),
            ),
            call=_audit(response),
        )


def _batch_inputs(
    inputs: tuple[str, ...],
    *,
    max_batch_size: int,
    max_batch_chars: int,
) -> tuple[tuple[str, ...], ...]:
    """在条数和字符双重预算内顺序切分 embedding 输入。

    Args:
        inputs: 保持原始顺序的 embedding 文本。
        max_batch_size: 单批允许的最大文本条数。
        max_batch_chars: 单批允许的最大字符总数。

    Returns:
        保持输入顺序且不超过任一预算的批次。

    Raises:
        ValueError: 单条文本已经超过字符预算。

    """
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_chars = 0
    for item in inputs:
        item_chars = len(item)
        if item_chars > max_batch_chars:
            raise ValueError("单条 embedding 输入超过字符预算。")
        if current and (
            len(current) >= max_batch_size
            or current_chars + item_chars > max_batch_chars
        ):
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _parse_embedding_payload(
    payload: object,
    *,
    expected_model: str,
    expected_count: int,
    dimension: int,
) -> tuple[tuple[float, ...], ...]:
    """校验 embedding 响应并恢复请求顺序。

    Args:
        payload: 服务返回的未信任 JSON 值。
        expected_model: 请求时冻结的 embedding 模型。
        expected_count: 请求文本总数。
        dimension: 每个向量必须具有的维度。

    Returns:
        按连续响应索引排序的有限浮点向量。

    Raises:
        ValueError: schema、模型、索引、维度或数值不符合契约。

    """
    if not isinstance(payload, dict) or not isinstance(
        payload.get("data"),
        list,
    ):
        raise ValueError("embedding 响应缺少 data 列表。")
    if payload.get("model") != expected_model:
        raise ValueError("embedding 响应模型与请求不一致。")
    indexed: dict[int, tuple[float, ...]] = {}
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise ValueError("embedding data 项格式无效。")
        index = item.get("index")
        raw_vector = item.get("embedding")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(raw_vector, list)
        ):
            raise ValueError("embedding 索引或向量格式无效。")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            for value in raw_vector
        ):
            raise ValueError("embedding 向量必须只含数值。")
        vector = tuple(float(value) for value in raw_vector)
        if len(vector) != dimension:
            raise ValueError("embedding 向量维度不一致。")
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding 向量含非有限数值。")
        if index in indexed:
            raise ValueError("embedding 响应含重复索引。")
        indexed[index] = vector
    if set(indexed) != set(range(expected_count)):
        raise ValueError("embedding 响应索引不完整。")
    return tuple(indexed[index] for index in range(expected_count))


def _validate_embedding_payload(
    payload: object,
    *,
    expected_model: str,
    expected_count: int,
    dimension: int,
) -> object:
    _parse_embedding_payload(
        payload,
        expected_model=expected_model,
        expected_count=expected_count,
        dimension=dimension,
    )
    return payload


def _parse_rerank_payload(
    payload: object,
    *,
    expected_count: int,
) -> tuple[RerankItem, ...]:
    """校验 rerank 响应并恢复候选顺序。

    Args:
        payload: 服务返回的未信任 JSON 值。
        expected_count: 请求候选文档总数。

    Returns:
        按连续候选索引排序的精排结果。

    Raises:
        ValueError: schema、索引或分数不符合精排契约。

    """
    if not isinstance(payload, dict) or not isinstance(
        payload.get("results"),
        list,
    ):
        raise ValueError("rerank 响应缺少 results 列表。")
    indexed: dict[int, RerankItem] = {}
    for raw_item in payload["results"]:
        if not isinstance(raw_item, dict):
            raise ValueError("rerank result 项格式无效。")
        index = raw_item.get("index")
        raw_score = raw_item.get("score")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("rerank index 格式无效。")
        if not isinstance(raw_score, (int, float)) or isinstance(
            raw_score,
            bool,
        ):
            raise ValueError("rerank score 格式无效。")
        score = float(raw_score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("rerank score 必须是 [0, 1] 有限数值。")
        if index in indexed:
            raise ValueError("rerank 响应含重复索引。")
        indexed[index] = RerankItem(index=index, score=score)
    if set(indexed) != set(range(expected_count)):
        raise ValueError("rerank 响应索引不完整。")
    return tuple(indexed[index] for index in range(expected_count))


def _validate_rerank_payload(
    payload: object,
    *,
    expected_count: int,
) -> object:
    _parse_rerank_payload(payload, expected_count=expected_count)
    return payload


def _apply_instruction(text: str, instruction: str) -> str:
    stripped_text = text.strip()
    stripped_instruction = instruction.strip()
    if not stripped_instruction:
        return stripped_text
    return f"Instruct: {stripped_instruction}\nText: {stripped_text}"


def _authorization_headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _audit(response: HttpJsonResponse) -> ExternalCallAudit:
    return ExternalCallAudit(
        endpoint=response.endpoint,
        retry_count=response.retry_count,
        elapsed_seconds=response.elapsed_seconds,
    )
