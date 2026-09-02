"""默认拒绝的数据出网和 Provider 路由策略。"""

from __future__ import annotations

from pydantic import Field, StrictInt

from rag_app.core.models.common import FrozenModel


class EgressPolicy(FrozenModel):
    """分别授权每类数据和每个远程目的地。"""

    remote_document_embedding: bool = False
    remote_query_embedding: bool = False
    remote_reranking: bool = False
    remote_generation: bool = False
    remote_document_embedding_jina: bool = False
    remote_query_embedding_jina: bool = False
    remote_reranking_jina: bool = False
    remote_document_embedding_aliyun: bool = False
    remote_query_embedding_aliyun: bool = False
    allow_aliyun_embedding_failover: bool = False
    aliyun_daily_request_budget: StrictInt = Field(default=0, ge=0)
    aliyun_daily_token_budget: StrictInt = Field(default=0, ge=0)


class CircuitBreakerPolicy(FrozenModel):
    """V1 默认 circuit breaker 候选值。"""

    failure_threshold: StrictInt = Field(default=2, gt=0)
    open_cooldown_seconds: StrictInt = Field(default=60, gt=0)
    half_open_max_calls: StrictInt = Field(default=1, gt=0)
    recovery_success_threshold: StrictInt = Field(default=3, gt=0)
    primary_preferred: bool = True
    background_paid_probe: bool = False
