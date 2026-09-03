"""不读取正文的有限 metadata/access filter。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rag_app.core.errors import PolicyDenied
from rag_app.core.models import ChannelHit, SearchRequest

_METADATA_KEYS = frozenset({"document_id", "section_id", "role"})
_ACCESS_KEYS = frozenset({"allowed_document_ids"})


def apply_candidate_filters(
    hits: Iterable[ChannelHit], request: SearchRequest
) -> tuple[ChannelHit, ...]:
    """在 canonical hydration 前以候选身份执行 allow-list 过滤。

    Args:
        hits: 不含正文的通道候选。
        request: 包含 metadata/access filters 的查询。

    Returns:
        仅保留显式允许身份的候选。

    Raises:
        PolicyDenied: filter 名称或值不在 P07 allow-list 中。

    """
    metadata = dict(request.metadata_filters)
    access = dict(request.access_filters)
    unknown = (set(metadata) - _METADATA_KEYS) | (set(access) - _ACCESS_KEYS)
    if unknown:
        raise PolicyDenied(
            "查询包含尚未支持的 filter，已失败关闭。",
            stage="retrieval.filters",
            details={"filter_names": sorted(unknown)},
        )
    allowed_documents = _values(access.get("allowed_document_ids"))
    result = []
    for hit in hits:
        if allowed_documents and hit.document_id not in allowed_documents:
            continue
        if not all(
            _matches(hit, name, expected) for name, expected in metadata.items()
        ):
            continue
        result.append(hit)
    return tuple(result)


def _matches(hit: ChannelHit, name: str, expected: object) -> bool:
    actual = getattr(hit, name)
    values = _values(expected)
    return not values or actual in values


def _values(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, Sequence):
        return frozenset(str(item) for item in value)
    raise PolicyDenied(
        "filter 值必须是字符串或字符串序列。",
        stage="retrieval.filters",
    )


__all__ = ["apply_candidate_filters"]
