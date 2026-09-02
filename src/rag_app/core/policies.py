"""格式解析、数据出网和 Provider 路由策略。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, StrictFloat, StrictInt, field_validator

from rag_app.core._base import FrozenModel
from rag_app.core.identifiers import canonical_json

_MAX_HEADING_LEVEL = 9


class ParsingMode(StrEnum):
    """解析错误处理模式。"""

    STRICT = "strict"
    BEST_EFFORT = "best_effort"


class TrackedChangesPolicy(StrEnum):
    """修订内容处理策略。"""

    FINAL_VIEW = "final_view"
    ALL_WITH_MARKERS = "all_with_markers"
    REJECT = "reject"


class CommentsPolicy(StrEnum):
    """批注处理策略。"""

    METADATA_ONLY = "metadata_only"
    INCLUDE = "include"
    REJECT = "reject"


class HiddenTextPolicy(StrEnum):
    """隐藏文字处理策略。"""

    EXCLUDE = "exclude"
    INCLUDE = "include"
    REJECT = "reject"


class StoryPolicy(StrEnum):
    """页眉页脚、脚注和尾注处理策略。"""

    PARSE = "parse"
    METADATA_ONLY = "metadata_only"
    EXCLUDE = "exclude"


class ImagesPolicy(StrEnum):
    """图片处理策略。"""

    METADATA = "metadata"
    EXTRACT = "extract"
    REJECT = "reject"


class ExternalRelationshipsPolicy(StrEnum):
    """外部关系处理策略。"""

    METADATA_ONLY = "metadata_only"
    REJECT = "reject"


class UnknownIndexableContentPolicy(StrEnum):
    """未知可索引内容处理策略。"""

    REJECT = "reject"
    ISSUE = "issue"


class ParsingPolicy(FrozenModel):
    """不允许 best-effort 放宽资源上限的严格解析策略。"""

    schema_version: str = Field(default="1", pattern=r"^1$")
    policy_id: str = Field(default="docx-safe-v1", min_length=1)
    mode: ParsingMode = ParsingMode.STRICT
    tracked_changes: TrackedChangesPolicy = TrackedChangesPolicy.FINAL_VIEW
    comments: CommentsPolicy = CommentsPolicy.METADATA_ONLY
    hidden_text: HiddenTextPolicy = HiddenTextPolicy.EXCLUDE
    headers_footers: StoryPolicy = StoryPolicy.METADATA_ONLY
    footnotes_endnotes: StoryPolicy = StoryPolicy.METADATA_ONLY
    images: ImagesPolicy = ImagesPolicy.METADATA
    external_relationships: ExternalRelationshipsPolicy = (
        ExternalRelationshipsPolicy.METADATA_ONLY
    )
    unknown_indexable_content: UnknownIndexableContentPolicy = (
        UnknownIndexableContentPolicy.REJECT
    )
    max_file_bytes: StrictInt = Field(default=128 * 1024 * 1024, gt=0)
    max_uncompressed_bytes: StrictInt = Field(
        default=512 * 1024 * 1024,
        gt=0,
    )
    max_entry_bytes: StrictInt = Field(default=64 * 1024 * 1024, gt=0)
    max_entries: StrictInt = Field(default=10_000, gt=0)
    max_compression_ratio: StrictFloat = Field(default=200.0, gt=1.0)
    parse_timeout_seconds: StrictFloat = Field(default=30.0, gt=0.0)
    max_xml_depth: StrictInt = Field(default=256, gt=0, le=2048)
    max_xml_nodes: StrictInt = Field(default=1_000_000, gt=0)
    max_table_depth: StrictInt = Field(default=32, gt=0, le=128)
    max_field_depth: StrictInt = Field(default=32, gt=0, le=128)
    custom_heading_styles: tuple[tuple[str, StrictInt], ...] = ()
    preserve_soft_hyphen: bool = False

    def canonical_json(self) -> str:
        """返回可进入 index fingerprint 的规范化策略。

        Args:
            无参数；读取当前冻结策略。

        Returns:
            不含绝对路径或 secret 的规范化 JSON。

        """
        return canonical_json(self.model_dump(mode="json", exclude_none=False))

    @field_validator("custom_heading_styles")
    @classmethod
    def _validate_custom_heading_styles(
        cls,
        value: tuple[tuple[str, StrictInt], ...],
    ) -> tuple[tuple[str, StrictInt], ...]:
        names = [name.casefold() for name, _ in value]
        if any(not name.strip() for name, _ in value):
            raise ValueError("custom heading style 名称不能为空。")
        if len(names) != len(set(names)):
            raise ValueError("custom heading style 名称禁止重复。")
        if any(
            level < 1 or level > _MAX_HEADING_LEVEL
            for _, level in value
        ):
            raise ValueError("custom heading style 层级必须在一到九之间。")
        return value


# P01 公共名称继续指向 P03 的正式策略，避免旧宿主立即迁移。
ParsePolicy = ParsingPolicy


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
