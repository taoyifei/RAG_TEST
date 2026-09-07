"""同步、一次有界改写的纯数据结果。"""

from dataclasses import dataclass
from typing import Protocol

from rag_app.core.models import ProviderCall, QueryVariant, SearchRequest


@dataclass(frozen=True)
class RewriteOutcome:
    """原问题保留，变体只能用于补充召回。"""

    variant: QueryVariant | None = None
    calls: tuple[ProviderCall, ...] = ()
    reason_code: str = "REWRITE_NOT_NEEDED"
    attempted: bool = False


class QueryRewritePort(Protocol):
    """不带基础设施依赖的按需改写端口。"""

    def rewrite(
        self, request: SearchRequest, *, recall_insufficient: bool = False
    ) -> RewriteOutcome:
        """至多一次调用；不满足对象、数字与否定合同则放弃。

        Args:
            request: 原问题及其已鉴权范围。
            recall_insufficient: 是否由首轮召回不足触发。

        Returns:
            可选补充变体与实际调用计量。

        """
        ...
