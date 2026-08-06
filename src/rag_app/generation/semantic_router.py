"""复用检索 query vector 的可校准语义问题路由。"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rag_app.clients.model_services import TeiEmbeddingClient
from rag_app.generation.question_profile import (
    PrimaryOperation,
    QuestionProfile,
    RouteSource,
    StructuralSignals,
)
from rag_app.state.intent_router_cache import (
    CachedPrototype,
    IntentRouterCache,
    PrototypeNamespace,
)
from rag_app.strict_json import load_json_file

__all__ = [
    "LLM_CLASSIFIER_CONTRACT_REVISION",
    "QUESTION_PROFILE_SCHEMA_REVISION",
    "IntentRouterConfig",
    "IntentRouterMode",
    "Prototype",
    "PrototypeWarmup",
    "QuestionProfileCalibration",
    "SemanticQuestionRouter",
    "load_intent_router_config",
    "load_question_profile_calibration",
]

QUESTION_PROFILE_SCHEMA_REVISION = "question-profile-v1"
LLM_CLASSIFIER_CONTRACT_REVISION = "intent-classifier-v1"
_MAX_WARMUP_BATCH_SIZE = 8
_MAX_LLM_FALLBACK_OUTPUT_TOKENS = 96
_MIN_PROTOTYPES_PER_OPERATION = 20
_ALLOWED_AGGREGATIONS = frozenset({"max", "mean_top_k", "max_mean"})


class IntentRouterMode(StrEnum):
    """语义路由的部署和灰度模式。"""

    LEGACY = "legacy"
    SHADOW = "shadow"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class Prototype:
    """不含答案的稳定 prototype 样本。"""

    example_id: str
    text: str

    def __post_init__(self) -> None:
        """拒绝空 ID 或空样本。"""
        if not self.example_id.strip() or not self.text.strip():
            raise ValueError("prototype ID 和文本不能为空。")


@dataclass(frozen=True, slots=True)
class IntentRouterConfig:
    """不携带拍脑袋阈值的受控路由配置。"""

    router_revision: str
    mode: IntentRouterMode
    aggregation_candidates: tuple[str, ...]
    top_k_candidates: tuple[int, ...]
    llm_fallback_enabled: bool
    llm_fallback_max_output_tokens: int
    operations: tuple[tuple[PrimaryOperation, tuple[Prototype, ...]], ...]
    canonical_sha256: str

    def __post_init__(self) -> None:
        """校验运行模式、样本规模和跨 operation 的规范化去重。"""
        if not self.router_revision.strip() or not self.canonical_sha256:
            raise ValueError("router revision 和 canonical SHA256 不能为空。")
        if (
            not self.aggregation_candidates
            or any(
                candidate not in _ALLOWED_AGGREGATIONS
                for candidate in self.aggregation_candidates
            )
            or len(set(self.aggregation_candidates))
            != len(self.aggregation_candidates)
        ):
            raise ValueError("aggregation_candidates 无效。")
        if (
            not self.top_k_candidates
            or any(candidate <= 0 for candidate in self.top_k_candidates)
            or len(set(self.top_k_candidates)) != len(self.top_k_candidates)
        ):
            raise ValueError("top_k_candidates 无效。")
        if (
            not 1
            <= self.llm_fallback_max_output_tokens
            <= (_MAX_LLM_FALLBACK_OUTPUT_TOKENS)
        ):
            raise ValueError("LLM fallback token 上限必须在 [1, 96] 内。")
        operation_keys = tuple(operation for operation, _ in self.operations)
        if set(operation_keys) != set(PrimaryOperation):
            raise ValueError("operations 必须恰好覆盖全部 primary operation。")
        normalized: set[str] = set()
        example_ids: set[str] = set()
        for _, prototypes in self.operations:
            if len(prototypes) < _MIN_PROTOTYPES_PER_OPERATION:
                raise ValueError("每个 operation 至少需要 20 条 prototype。")
            for prototype in prototypes:
                if prototype.example_id in example_ids:
                    raise ValueError(
                        "prototype example ID 不能跨 operation 重复。"
                    )
                example_ids.add(prototype.example_id)
                value = _normalize_text(prototype.text)
                if value in normalized:
                    raise ValueError(
                        "prototype 规范化文本不能跨 operation 重复。"
                    )
                normalized.add(value)

    @property
    def example_count(self) -> int:
        """返回全部 prototype 条数。

        Args:
            无参数；统计当前不可变 operations。

        Returns:
            稳定的样本总数。

        """
        return sum(len(prototypes) for _, prototypes in self.operations)

    def prototypes(self) -> tuple[tuple[PrimaryOperation, Prototype], ...]:
        """按 operation 和 ID 稳定返回全部样本。

        Args:
            无参数；读取受控配置样本。

        Returns:
            供预热批量嵌入的 operation/prototype 元组。

        """
        return tuple(
            (operation, prototype)
            for operation, items in self.operations
            for prototype in items
        )


@dataclass(frozen=True, slots=True)
class QuestionProfileCalibration:
    """真实 embedding 校准后才能启用的阈值产物。"""

    status: str
    canonical_sha256: str = ""
    intent_config_sha256: str | None = None
    embedding_model: str | None = None
    embedding_revision: str | None = None
    tokenizer_sha256: str | None = None
    vector_dimension: int | None = None
    aggregation: str | None = None
    top_k: int | None = None
    min_score: float | None = None
    min_margin: float | None = None
    secondary_min_score: float | None = None
    secondary_max_gap: float | None = None

    @property
    def verified(self) -> bool:
        """判断产物是否提供了全部可用阈值。

        Args:
            无参数；检查当前校准身份和阈值。

        Returns:
            只有完整 verified 产物才返回真。

        """
        return (
            self.status == "verified"
            and self.intent_config_sha256 is not None
            and self.embedding_model is not None
            and self.embedding_revision is not None
            and self.tokenizer_sha256 is not None
            and self.vector_dimension is not None
            and self.aggregation in _ALLOWED_AGGREGATIONS
            and self.top_k is not None
            and self.min_score is not None
            and self.min_margin is not None
            and self.secondary_min_score is not None
            and self.secondary_max_gap is not None
        )

    def matches(self, namespace: PrototypeNamespace) -> bool:
        """判断校准是否精确绑定当前配置和 embedding 身份。

        Args:
            namespace: 当前 prototype 缓存的联合身份。

        Returns:
            校准可安全用于该 namespace 时返回真。

        """
        if not self.verified:
            return False
        return (
            self.intent_config_sha256 == namespace.config_sha256
            and self.embedding_model == namespace.embedding_model
            and self.embedding_revision == namespace.embedding_revision
            and self.tokenizer_sha256 == namespace.tokenizer_sha256
            and self.vector_dimension == namespace.dimension
        )


class SemanticQuestionRouter:
    """只在内存中对已存在的 query vector 进行相似度路由。"""

    def __init__(
        self,
        *,
        config: IntentRouterConfig,
        calibration: QuestionProfileCalibration,
        namespace: PrototypeNamespace,
    ) -> None:
        """保存受控配置、校准身份和空的内存 prototype 快照。

        Args:
            config: 路由模式和 prototype 配置。
            calibration: 实际校准产物；未校准时维持 GENERAL。
            namespace: 与配置和 embedding 绑定的缓存身份。

        Returns:
            无返回值。

        """
        self._config = config
        self._calibration = calibration
        self._namespace = namespace
        self._lock = threading.Lock()
        self._prototypes: tuple[CachedPrototype, ...] = ()

    @property
    def config(self) -> IntentRouterConfig:
        """返回不可变路由配置。

        Args:
            无参数；读取当前配置。

        Returns:
            当前 `IntentRouterConfig`。

        """
        return self._config

    @property
    def namespace(self) -> PrototypeNamespace:
        """返回当前缓存身份。

        Args:
            无参数；读取缓存 namespace。

        Returns:
            当前 `PrototypeNamespace`。

        """
        return self._namespace

    @property
    def prototype_cache_ready(self) -> bool:
        """返回完整 prototype 快照是否已在内存中。

        Args:
            无参数；读取保护后的快照状态。

        Returns:
            可参与路由时返回真。

        """
        with self._lock:
            return (
                len(self._prototypes) == self._namespace.expected_example_count
            )

    def set_prototypes(self, prototypes: tuple[CachedPrototype, ...]) -> None:
        """原子替换完整、已校验的内存 prototype 快照。

        Args:
            prototypes: 只含 prototype vector 的完整 namespace。

        Returns:
            无返回值。

        Raises:
            ValueError: 样本身份或维度不符合当前 namespace。

        """
        if len(prototypes) != self._namespace.expected_example_count:
            raise ValueError("prototype 缓存数量不完整。")
        if any(
            len(prototype.vector) != self._namespace.dimension
            for prototype in prototypes
        ):
            raise ValueError("prototype 缓存维度不匹配。")
        with self._lock:
            self._prototypes = tuple(prototypes)

    def route(  # noqa: PLR0911
        self,
        resolved_query: str,
        query_vector: tuple[float, ...] | None,
        structural_signals: StructuralSignals,
    ) -> QuestionProfile:
        """基于已存在的 query vector 生成语义 profile。

        Args:
            resolved_query: 已重写完成的查询，仅用于非空边界校验。
            query_vector: 检索阶段复用的纯内存向量；不会在此处嵌入。
            structural_signals: 高精度槽位和锚点信号。

        Returns:
            置信度不足、缓存缺失或校准无效时使用 GENERAL 的 profile。

        """
        if not resolved_query.strip():
            return _general_profile(
                structural_signals,
                reason_code="EMPTY_RESOLVED_QUERY",
            )
        if query_vector is None:
            return _general_profile(
                structural_signals,
                reason_code="QUERY_VECTOR_UNAVAILABLE",
            )
        if not _valid_vector(query_vector, self._namespace.dimension):
            return _general_profile(
                structural_signals,
                reason_code="QUERY_VECTOR_INVALID",
            )
        if not self._calibration.matches(self._namespace):
            return _general_profile(
                structural_signals,
                reason_code="CALIBRATION_UNVERIFIED",
            )
        thresholds = _required_thresholds(self._calibration)
        if thresholds is None:
            return _general_profile(
                structural_signals,
                reason_code="CALIBRATION_UNVERIFIED",
            )
        (
            min_score,
            min_margin,
            secondary_min_score,
            secondary_max_gap,
        ) = thresholds
        with self._lock:
            prototypes = self._prototypes
        if len(prototypes) != self._namespace.expected_example_count:
            return _general_profile(
                structural_signals,
                reason_code="PROTOTYPE_CACHE_UNAVAILABLE",
            )
        scores = _operation_scores(
            query_vector,
            prototypes,
            aggregation=self._calibration.aggregation,
            top_k=self._calibration.top_k,
        )
        if not scores:
            return _general_profile(
                structural_signals,
                reason_code="SEMANTIC_SCORE_UNAVAILABLE",
            )
        primary, confidence = scores[0]
        margin = confidence - (scores[1][1] if len(scores) > 1 else 0.0)
        if confidence < min_score or margin < min_margin:
            return QuestionProfile(
                primary_operation=PrimaryOperation.GENERAL,
                secondary_operations=(),
                requested_slots=structural_signals.requested_slots,
                confidence=confidence,
                margin=max(0.0, margin),
                route_source=RouteSource.GENERAL,
                scores=scores,
                fallback_used=True,
                reason_code="SEMANTIC_UNCERTAIN",
            )
        secondary = tuple(
            operation
            for operation, score in scores[1:]
            if (
                operation is not PrimaryOperation.GENERAL
                and score >= secondary_min_score
                and confidence - score <= secondary_max_gap
            )
        )[:2]
        return QuestionProfile(
            primary_operation=primary,
            secondary_operations=secondary,
            requested_slots=structural_signals.requested_slots,
            confidence=confidence,
            margin=max(0.0, margin),
            route_source=RouteSource.SEMANTIC,
            scores=scores,
            fallback_used=False,
            reason_code="SEMANTIC_CONFIDENT",
        )


class PrototypeWarmup:
    """命中缓存即加载，否则在后台有界生成 prototype 向量。"""

    def __init__(
        self,
        *,
        cache: IntentRouterCache,
        router: SemanticQuestionRouter,
        embedding: TeiEmbeddingClient,
        instruction: str,
    ) -> None:
        """保存预热依赖，不在构造时访问网络。

        Args:
            cache: 独立 prototype SQLite 缓存。
            router: 接收完整内存快照的路由器。
            embedding: 复用 query embedding 路径的客户端。
            instruction: 与检索完全相同的 query instruction。

        Returns:
            无返回值。

        """
        self._cache = cache
        self._router = router
        self._embedding = embedding
        self._instruction = instruction
        self._started = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """同步加载完整缓存，未命中时启动后台预热。

        Args:
            无参数；不会等待 embedding 网络请求完成。

        Returns:
            无返回值。

        """
        if self._started.is_set():
            return
        self._started.set()
        cached = self._cache.load_complete(self._router.namespace)
        if cached:
            self._router.set_prototypes(cached)
            return
        self._thread = threading.Thread(
            target=self._warm,
            name="intent-router-warmup",
            daemon=True,
        )
        self._thread.start()

    def _warm(self) -> None:
        """后台生成完整 prototype vector，并且只在全部成功后发布。"""
        try:
            examples = self._router.config.prototypes()
            vectors: list[CachedPrototype] = []
            for batch in _batches(examples, _MAX_WARMUP_BATCH_SIZE):
                result = self._embedding.embed(
                    tuple(prototype.text for _, prototype in batch),
                    instruction=self._instruction,
                )
                if len(result.vectors) != len(batch):
                    return
                vectors.extend(
                    CachedPrototype(
                        example_id=prototype.example_id,
                        operation=operation,
                        text_sha256=hashlib.sha256(
                            prototype.text.encode("utf-8")
                        ).hexdigest(),
                        vector=vector,
                    )
                    for (operation, prototype), vector in zip(
                        batch,
                        result.vectors,
                        strict=True,
                    )
                )
            complete = tuple(vectors)
            self._cache.publish(self._router.namespace, complete)
            self._router.set_prototypes(complete)
        except Exception:
            # 预热失败必须保持空快照；不能让后台异常影响查询主链。
            return


def load_intent_router_config(path: Path) -> IntentRouterConfig:
    """加载严格 JSON 路由配置，并计算 canonical SHA256。

    Args:
        path: `intent-router.json` 的本地绝对路径。

    Returns:
        已验证的不可变路由配置。

    Raises:
        ValueError: 配置字段、样本或模式不满足当前 shadow 合同。

    """
    payload = load_json_file(path, label="intent router")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "router_revision",
        "mode",
        "aggregation_candidates",
        "top_k_candidates",
        "llm_fallback",
        "operations",
    }:
        raise ValueError("intent router 配置字段不完整或包含未知字段。")
    if payload["schema_version"] != 1:
        raise ValueError("intent router schema_version 必须为 1。")
    llm_fallback = payload["llm_fallback"]
    if not isinstance(llm_fallback, dict) or set(llm_fallback) != {
        "enabled",
        "max_output_tokens",
    }:
        raise ValueError("intent router llm_fallback 字段无效。")
    if not isinstance(llm_fallback["enabled"], bool):
        raise ValueError("intent router llm_fallback.enabled 必须是布尔值。")
    raw_operations = payload["operations"]
    if not isinstance(raw_operations, dict):
        raise ValueError("intent router operations 必须是对象。")
    operations = tuple(
        (
            operation,
            _parse_prototypes(raw_operations.get(operation.value), operation),
        )
        for operation in PrimaryOperation
    )
    canonical = _canonical_sha256(payload)
    return IntentRouterConfig(
        router_revision=_string_field(payload, "router_revision"),
        mode=IntentRouterMode(_string_field(payload, "mode")),
        aggregation_candidates=_string_tuple(
            payload["aggregation_candidates"],
            label="aggregation_candidates",
        ),
        top_k_candidates=_positive_int_tuple(
            payload["top_k_candidates"],
            label="top_k_candidates",
        ),
        llm_fallback_enabled=llm_fallback["enabled"],
        llm_fallback_max_output_tokens=_positive_int(
            llm_fallback["max_output_tokens"],
            label="llm_fallback.max_output_tokens",
        ),
        operations=operations,
        canonical_sha256=canonical,
    )


def load_question_profile_calibration(
    path: Path,
) -> QuestionProfileCalibration:
    """加载未校准或已校准的语义路由阈值产物。

    Args:
        path: `intent-router-calibration.json` 的本地路径。

    Returns:
        不会凭空补齐阈值的校准对象。

    Raises:
        ValueError: JSON 字段与 status 的合同不一致。

    """
    payload = load_json_file(path, label="intent router calibration")
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("intent router calibration schema_version 必须为 1。")
    status = payload.get("status")
    if status == "unverified" and set(payload) == {
        "schema_version",
        "status",
    }:
        return QuestionProfileCalibration(
            status="unverified",
            canonical_sha256=_canonical_sha256(payload),
        )
    required = {
        "schema_version",
        "status",
        "intent_config_sha256",
        "embedding_model",
        "embedding_revision",
        "tokenizer_sha256",
        "vector_dimension",
        "aggregation",
        "top_k",
        "min_score",
        "min_margin",
        "secondary_min_score",
        "secondary_max_gap",
        "tuning_dataset_sha256",
        "tuning_metrics",
        "generated_at",
    }
    if status != "verified" or set(payload) != required:
        raise ValueError("intent router calibration 状态或字段无效。")
    calibration = QuestionProfileCalibration(
        status="verified",
        canonical_sha256=_canonical_sha256(payload),
        intent_config_sha256=_string_field(payload, "intent_config_sha256"),
        embedding_model=_string_field(payload, "embedding_model"),
        embedding_revision=_string_field(payload, "embedding_revision"),
        tokenizer_sha256=_string_field(payload, "tokenizer_sha256"),
        vector_dimension=_positive_int(
            payload["vector_dimension"],
            label="vector_dimension",
        ),
        aggregation=_string_field(payload, "aggregation"),
        top_k=_positive_int(payload["top_k"], label="top_k"),
        min_score=_probability(payload["min_score"], label="min_score"),
        min_margin=_probability(payload["min_margin"], label="min_margin"),
        secondary_min_score=_probability(
            payload["secondary_min_score"],
            label="secondary_min_score",
        ),
        secondary_max_gap=_probability(
            payload["secondary_max_gap"],
            label="secondary_max_gap",
        ),
    )
    if not calibration.verified:
        raise ValueError("intent router calibration 阈值无效。")
    return calibration


def _parse_prototypes(
    value: object,
    operation: PrimaryOperation,
) -> tuple[Prototype, ...]:
    if not isinstance(value, dict) or set(value) != {"description", "examples"}:
        raise ValueError(f"{operation.value} prototype 配置无效。")
    examples = value["examples"]
    if not isinstance(examples, list):
        raise ValueError(f"{operation.value} examples 必须是数组。")
    parsed = tuple(
        Prototype(
            example_id=_string_field(item, "id"),
            text=_string_field(item, "text"),
        )
        for item in examples
        if isinstance(item, dict) and set(item) == {"id", "text"}
    )
    if len(parsed) != len(examples):
        raise ValueError(f"{operation.value} prototype 字段无效。")
    return parsed


def _general_profile(
    structural_signals: StructuralSignals,
    *,
    reason_code: str,
) -> QuestionProfile:
    return QuestionProfile(
        primary_operation=PrimaryOperation.GENERAL,
        secondary_operations=(),
        requested_slots=structural_signals.requested_slots,
        confidence=0.0,
        margin=0.0,
        route_source=RouteSource.GENERAL,
        scores=(),
        fallback_used=True,
        reason_code=reason_code,
    )


def _required_thresholds(
    calibration: QuestionProfileCalibration,
) -> tuple[float, float, float, float] | None:
    values = (
        calibration.min_score,
        calibration.min_margin,
        calibration.secondary_min_score,
        calibration.secondary_max_gap,
    )
    if any(value is None for value in values):
        return None
    resolved = [float(value) for value in values if value is not None]
    return (
        resolved[0],
        resolved[1],
        resolved[2],
        resolved[3],
    )


def _operation_scores(
    query_vector: tuple[float, ...],
    prototypes: tuple[CachedPrototype, ...],
    *,
    aggregation: str | None,
    top_k: int | None,
) -> tuple[tuple[PrimaryOperation, float], ...]:
    if aggregation not in _ALLOWED_AGGREGATIONS or top_k is None or top_k <= 0:
        return ()
    query_norm = _normalize_vector(query_vector)
    if query_norm is None:
        return ()
    grouped: dict[PrimaryOperation, list[float]] = {
        operation: [] for operation in PrimaryOperation
    }
    for prototype in prototypes:
        prototype_norm = _normalize_vector(prototype.vector)
        if prototype_norm is None:
            return ()
        grouped[prototype.operation].append(
            sum(
                left * right
                for left, right in zip(query_norm, prototype_norm, strict=True)
            )
        )
    scores = tuple(
        (
            operation,
            _aggregate(sorted(values, reverse=True)[:top_k], aggregation),
        )
        for operation, values in grouped.items()
        if values
    )
    return tuple(sorted(scores, key=lambda item: (-item[1], item[0].value)))


def _aggregate(values: list[float], aggregation: str) -> float:
    if not values:
        return 0.0
    maximum = values[0]
    mean = sum(values) / len(values)
    if aggregation == "max":
        return maximum
    if aggregation == "mean_top_k":
        return mean
    return 0.7 * maximum + 0.3 * mean


def _valid_vector(vector: tuple[float, ...], dimension: int) -> bool:
    return (
        len(vector) == dimension
        and all(math.isfinite(value) for value in vector)
        and _normalize_vector(vector) is not None
    )


def _normalize_vector(vector: tuple[float, ...]) -> tuple[float, ...] | None:
    squared_norm = sum(value * value for value in vector)
    if not math.isfinite(squared_norm) or squared_norm <= 0.0:
        return None
    norm = math.sqrt(squared_norm)
    return tuple(value / norm for value in vector)


def _batches(
    values: tuple[tuple[PrimaryOperation, Prototype], ...],
    size: int,
) -> Iterable[tuple[tuple[PrimaryOperation, Prototype], ...]]:
    return (
        values[index : index + size] for index in range(0, len(values), size)
    )


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_text(value: str) -> str:
    return "".join(value.split()).casefold()


def _string_field(payload: object, key: str) -> str:
    if not isinstance(payload, dict):
        raise ValueError(f"{key} 所在对象无效。")
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串。")
    return value


def _string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} 必须是非空字符串数组。")
    return tuple(value)


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} 必须是正整数。")
    return value


def _positive_int_tuple(value: object, *, label: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是正整数数组。")
    return tuple(_positive_int(item, label=label) for item in value)


def _probability(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值。")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} 必须在 [0, 1] 内。")
    return result
