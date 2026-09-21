"""计算不改变实际检索范围的部门 Shadow Routing 建议。"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field

from rag_app.core.models.common import FrozenModel
from rag_app.wanshitong.department_profiles import (
    DepartmentEmbeddingIdentity,
    DepartmentProfile,
    DepartmentProfileSet,
)

DEPARTMENT_ROUTE_REVISION = "wanshitong-department-shadow-v1"

_LATIN_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_HIGH_SCORE_BUCKET_THRESHOLD = 0.5
_MEDIUM_SCORE_BUCKET_THRESHOLD = 0.3


class DepartmentRouteConfidence(StrEnum):
    """未校准的有限置信等级。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class DepartmentRecommendedScope(StrEnum):
    """只供观察的建议部门范围。"""

    TOP1 = "TOP1"
    TOP2 = "TOP2"
    GLOBAL = "GLOBAL"


class DepartmentRouteStatus(StrEnum):
    """本次 Shadow 观察是否完成计算。"""

    COMPUTED = "COMPUTED"
    DISABLED = "DISABLED"
    FALLBACK = "FALLBACK"


class DepartmentRoutingPolicy(FrozenModel):
    """首版集中阈值与权重；这些数值不代表准确率。"""

    lexical_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    vector_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    top1_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    top1_margin: float = Field(default=0.15, ge=0.0, le=1.0)
    top2_threshold: float = Field(default=0.3, ge=0.0, le=1.0)


class ReusableRootVector(FrozenModel):
    """检索主链已经生成且允许只读复用的根向量。"""

    identity: DepartmentEmbeddingIdentity
    values: tuple[float, ...] = Field(repr=False)


class DepartmentRouteSuggestion(FrozenModel):
    """不携带原问、向量或精确分数的只读路由建议。"""

    route_revision: str = Field(
        default=DEPARTMENT_ROUTE_REVISION,
        pattern=r"^wanshitong-department-shadow-v1$",
    )
    profile_revision: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    top1_department_key: str | None = None
    top1_score_bucket: str | None = None
    top2_department_key: str | None = None
    top2_score_bucket: str | None = None
    confidence: DepartmentRouteConfidence = DepartmentRouteConfidence.LOW
    recommended_scope: DepartmentRecommendedScope = (
        DepartmentRecommendedScope.GLOBAL
    )
    reason_codes: tuple[str, ...] = ()
    embedding_reused: bool = False
    department_filter_applied: bool = False
    extra_provider_calls: int = Field(default=0, ge=0, le=0)
    status: DepartmentRouteStatus = DepartmentRouteStatus.COMPUTED


@dataclass(frozen=True, slots=True)
class _ScoredProfile:
    profile: DepartmentProfile
    score: float


def suggest_department(  # noqa: PLR0911, PLR0913
    resolved_root_query: str,
    context_mode: str,
    profiles: DepartmentProfileSet,
    optional_root_vector: ReusableRootVector | None,
    explicit_source_scope: tuple[str, ...] = (),
    *,
    policy: DepartmentRoutingPolicy | None = None,
) -> DepartmentRouteSuggestion:
    """根据已有根问题、Profile 和可选根向量生成观察建议。

    Args:
        resolved_root_query: 已有上下文解析得到的根问题。
        context_mode: 已有上下文模式及置信描述。
        profiles: 与请求 Scope 和 Index Revision 匹配的 Profile。
        optional_root_vector: 已有 Dense 根向量；不得为 Shadow 新建。
        explicit_source_scope: 已解析 SourceScope 中的文档身份。
        policy: 可选首版集中权重与阈值。

    Returns:
        不会修改请求或检索范围的部门建议。

    """
    if not profiles.profiles:
        return fallback_department_suggestion(
            "PROFILE_EMPTY", profile_revision=profiles.profile_revision
        )
    normalized_context = context_mode.strip().upper()
    if "CLARIFY" in normalized_context or "LOW" in normalized_context:
        return fallback_department_suggestion(
            "CONTEXT_LOW_CONFIDENCE",
            profile_revision=profiles.profile_revision,
        )
    query = _normalize_text(resolved_root_query)
    if not query:
        return fallback_department_suggestion(
            "QUERY_EMPTY", profile_revision=profiles.profile_revision
        )
    route_policy = policy or DepartmentRoutingPolicy()
    explicit_matches = tuple(
        profile
        for profile in profiles.profiles
        if any(
            candidate and _normalize_text(candidate) in query
            for candidate in (profile.department_name, *profile.aliases)
        )
    )
    if len(explicit_matches) == 1:
        profile = explicit_matches[0]
        return DepartmentRouteSuggestion(
            profile_revision=profiles.profile_revision,
            top1_department_key=profile.department_key,
            top1_score_bucket="EXPLICIT",
            confidence=DepartmentRouteConfidence.HIGH,
            recommended_scope=DepartmentRecommendedScope.TOP1,
            reason_codes=("EXPLICIT_DEPARTMENT_UNIQUE",),
        )
    source_matches = tuple(
        profile
        for profile in profiles.profiles
        if set(explicit_source_scope).intersection(profile.document_ids)
    )
    if len(source_matches) == 1:
        profile = source_matches[0]
        return DepartmentRouteSuggestion(
            profile_revision=profiles.profile_revision,
            top1_department_key=profile.department_key,
            top1_score_bucket="EXPLICIT_SOURCE",
            confidence=DepartmentRouteConfidence.HIGH,
            recommended_scope=DepartmentRecommendedScope.TOP1,
            reason_codes=("EXPLICIT_SOURCE_DEPARTMENT",),
        )

    vector_usable, vector_reason = _vector_availability(
        profiles, optional_root_vector
    )
    scored = tuple(
        sorted(
            (
                _ScoredProfile(
                    profile=profile,
                    score=_combined_score(
                        query,
                        profile,
                        optional_root_vector=(
                            optional_root_vector if vector_usable else None
                        ),
                        policy=route_policy,
                    ),
                )
                for profile in profiles.profiles
            ),
            key=lambda item: (-item.score, item.profile.department_key),
        )
    )
    top1 = scored[0]
    top2 = scored[1] if len(scored) > 1 else None
    reasons: list[str] = []
    if len(explicit_matches) > 1:
        reasons.append("EXPLICIT_DEPARTMENT_AMBIGUOUS")
    if len(source_matches) > 1:
        reasons.append("EXPLICIT_SOURCE_MULTIPLE_DEPARTMENTS")
    reasons.append("LEXICAL_PROFILE_OVERLAP")
    reasons.append(vector_reason)
    if top1.score <= 0.0:
        return DepartmentRouteSuggestion(
            profile_revision=profiles.profile_revision,
            confidence=DepartmentRouteConfidence.LOW,
            recommended_scope=DepartmentRecommendedScope.GLOBAL,
            reason_codes=tuple(dict.fromkeys((*reasons, "ALL_SCORES_ZERO"))),
            embedding_reused=vector_usable,
            status=DepartmentRouteStatus.FALLBACK,
        )
    margin = top1.score - (0.0 if top2 is None else top2.score)
    confidence = DepartmentRouteConfidence.LOW
    recommended_scope = DepartmentRecommendedScope.GLOBAL
    if (
        len(explicit_matches) <= 1
        and top1.score >= route_policy.top1_threshold
        and margin >= route_policy.top1_margin
    ):
        confidence = DepartmentRouteConfidence.HIGH
        recommended_scope = DepartmentRecommendedScope.TOP1
        reasons.append("TOP1_THRESHOLD_AND_MARGIN")
    elif top2 is not None and top1.score >= route_policy.top2_threshold:
        confidence = DepartmentRouteConfidence.MEDIUM
        recommended_scope = DepartmentRecommendedScope.TOP2
        reasons.append("TOP2_THRESHOLD")
    else:
        reasons.append("LOW_CONFIDENCE_GLOBAL")
    return DepartmentRouteSuggestion(
        profile_revision=profiles.profile_revision,
        top1_department_key=top1.profile.department_key,
        top1_score_bucket=_score_bucket(top1.score),
        top2_department_key=(
            None if top2 is None else top2.profile.department_key
        ),
        top2_score_bucket=None if top2 is None else _score_bucket(top2.score),
        confidence=confidence,
        recommended_scope=recommended_scope,
        reason_codes=tuple(dict.fromkeys(reasons)),
        embedding_reused=vector_usable,
    )


def disabled_department_suggestion() -> DepartmentRouteSuggestion:
    """返回显式关闭状态，便于管理员区分未采集与关闭。"""
    return DepartmentRouteSuggestion(
        reason_codes=("DEPARTMENT_SHADOW_DISABLED",),
        status=DepartmentRouteStatus.DISABLED,
    )


def fallback_department_suggestion(
    reason_code: str, *, profile_revision: str | None = None
) -> DepartmentRouteSuggestion:
    """把 Profile 缺失、损坏或不匹配统一降级为 GLOBAL。"""
    return DepartmentRouteSuggestion(
        profile_revision=profile_revision,
        reason_codes=(reason_code,),
        status=DepartmentRouteStatus.FALLBACK,
    )


def _combined_score(
    query: str,
    profile: DepartmentProfile,
    *,
    optional_root_vector: ReusableRootVector | None,
    policy: DepartmentRoutingPolicy,
) -> float:
    query_tokens = _tokens(query)
    profile_tokens = _tokens(
        " ".join(
            (
                profile.department_name,
                *profile.aliases,
                *(
                    segment
                    for path in profile.category_paths
                    for segment in path
                ),
                *profile.document_titles,
                *profile.representative_headings,
                *profile.topic_keys,
            )
        )
    )
    lexical = (
        0.0
        if not query_tokens
        else len(query_tokens.intersection(profile_tokens)) / len(query_tokens)
    )
    if optional_root_vector is None or profile.vector is None:
        return lexical
    cosine = _cosine(optional_root_vector.values, profile.vector)
    vector_score = min(1.0, max(0.0, (cosine + 1.0) / 2.0))
    total_weight = policy.lexical_weight + policy.vector_weight
    if total_weight <= 0.0:
        return 0.0
    return (
        policy.lexical_weight * lexical + policy.vector_weight * vector_score
    ) / total_weight


def _vector_availability(
    profiles: DepartmentProfileSet,
    root_vector: ReusableRootVector | None,
) -> tuple[bool, str]:
    if root_vector is None:
        return False, "ROOT_VECTOR_UNAVAILABLE"
    identity = profiles.embedding_identity
    if identity is None or any(
        profile.vector is None for profile in profiles.profiles
    ):
        return False, "PROFILE_VECTOR_UNAVAILABLE"
    if (
        identity.vector_space_identity
        != root_vector.identity.vector_space_identity
    ):
        return False, "VECTOR_IDENTITY_MISMATCH"
    if len(root_vector.values) != identity.dimension or any(
        not math.isfinite(value) for value in root_vector.values
    ):
        return False, "ROOT_VECTOR_INVALID"
    return True, "ROOT_VECTOR_REUSED"


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _tokens(value: str) -> frozenset[str]:
    normalized = _normalize_text(value)
    tokens = set(_LATIN_TOKEN.findall(normalized))
    for match in _CJK_RUN.finditer(normalized):
        run = match[0]
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return frozenset(tokens)


def _score_bucket(score: float) -> str:
    if score >= _HIGH_SCORE_BUCKET_THRESHOLD:
        return "GE_0_50"
    if score >= _MEDIUM_SCORE_BUCKET_THRESHOLD:
        return "0_30_TO_0_49"
    if score > 0.0:
        return "LT_0_30"
    return "ZERO"


__all__ = [
    "DEPARTMENT_ROUTE_REVISION",
    "DepartmentRecommendedScope",
    "DepartmentRouteConfidence",
    "DepartmentRouteStatus",
    "DepartmentRouteSuggestion",
    "DepartmentRoutingPolicy",
    "ReusableRootVector",
    "disabled_department_suggestion",
    "fallback_department_suggestion",
    "suggest_department",
]
