"""状态、权威级别与有效期的确定性 Qdrant 预过滤。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from qdrant_client.http import models

__all__ = ["MetadataPolicy"]


@dataclass(frozen=True, slots=True)
class MetadataPolicy:
    """查询允许的文档状态与权威级别。"""

    allowed_statuses: tuple[str, ...]
    allowed_authority_levels: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝空值或重复过滤项。"""
        _validate_values("allowed_statuses", self.allowed_statuses)
        _validate_values(
            "allowed_authority_levels",
            self.allowed_authority_levels,
        )

    def to_qdrant_filter(
        self,
        *,
        as_of: datetime,
        source_ids: tuple[str, ...] = (),
    ) -> models.Filter:
        """构造有效期边界缺失即不受限的 Qdrant filter。

        Args:
            as_of: 带时区的确定性查询时点。
            source_ids: 高置信软路由选中的稳定来源；为空即全库。

        Returns:
            状态、权威与 `[effective_from, effective_to]` 联合过滤。

        Raises:
            ValueError: as_of 不含时区。

        """
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of 必须包含时区。")
        must: list[models.Condition] = [
                models.FieldCondition(
                    key="document_status",
                    match=models.MatchAny(any=list(self.allowed_statuses)),
                ),
                models.FieldCondition(
                    key="authority_level",
                    match=models.MatchAny(
                        any=list(self.allowed_authority_levels)
                    ),
                ),
                _date_boundary_filter(
                    field_name="effective_from",
                    boundary=models.DatetimeRange(lte=as_of),
                ),
                _date_boundary_filter(
                    field_name="effective_to",
                    boundary=models.DatetimeRange(gte=as_of),
                ),
        ]
        if source_ids:
            must.append(
                models.FieldCondition(
                    key="source_id",
                    match=models.MatchAny(any=list(source_ids)),
                )
            )
        return models.Filter(must=must)


def _date_boundary_filter(
    *,
    field_name: str,
    boundary: models.DatetimeRange,
) -> models.Filter:
    return models.Filter(
        should=[
            models.FieldCondition(
                key=field_name,
                range=boundary,
            ),
            models.IsEmptyCondition(
                is_empty=models.PayloadField(key=field_name)
            ),
        ]
    )


def _validate_values(field_name: str, values: tuple[str, ...]) -> None:
    if not values or any(not value for value in values):
        raise ValueError(f"{field_name} 必须包含非空值。")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} 不能重复。")
