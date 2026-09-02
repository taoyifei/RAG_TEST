"""DOCX v4 解析问题的聚合与安全输出。"""

from __future__ import annotations

from collections import Counter

from rag_app.adapters.parsers.docx.models import json_object
from rag_app.core.identifiers import canonical_json
from rag_app.core.models import ParseIssue, SourceAnchor


class IssueCollector:
    """按安全字段聚合重复 ParseIssue。"""

    def __init__(self) -> None:
        """创建空的问题集合。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self._issues: list[ParseIssue] = []
        self._unsupported_text = 0
        self._unsupported_media = 0

    @property
    def unsupported_text(self) -> int:
        """返回无法表示的可见文本节点数。

        Args:
            无参数；读取当前收集器。

        Returns:
            未表示文本计数。

        """
        return self._unsupported_text

    @property
    def unsupported_media(self) -> int:
        """返回无法表示的媒体节点数。

        Args:
            无参数；读取当前收集器。

        Returns:
            未表示媒体计数。

        """
        return self._unsupported_media

    def add(  # noqa: PLR0913
        self,
        code: str,
        *,
        severity: str = "warning",
        action: str,
        message: str,
        anchor: SourceAnchor | None = None,
        count: int = 1,
        unsupported_text: int = 0,
        unsupported_media: int = 0,
        metadata: tuple[tuple[str, object], ...] = (),
    ) -> None:
        """记录不含正文和敏感身份的问题。

        Args:
            code: 稳定机器错误码。
            severity: `info`、`warning` 或 `error`。
            action: 下游可审计动作。
            message: 不含正文的安全说明。
            anchor: 可选来源锚点。
            count: 本次问题数量。
            unsupported_text: 新增未表示文本节点数。
            unsupported_media: 新增未表示媒体节点数。
            metadata: 可选安全结构元数据。

        Returns:
            无返回值。

        """
        self._issues.append(
            ParseIssue(
                code=code,
                severity=severity,
                action=action,
                safe_message=message,
                anchor=anchor,
                count=count,
                metadata=json_object(metadata),
            )
        )
        self._unsupported_text += unsupported_text
        self._unsupported_media += unsupported_media

    def freeze(self) -> tuple[ParseIssue, ...]:
        """合并同类且同动作的问题。

        Args:
            无参数。

        Returns:
            按首次出现顺序排列的问题元组。

        """
        counts: Counter[tuple[object, ...]] = Counter()
        first: dict[tuple[object, ...], ParseIssue] = {}
        order: list[tuple[object, ...]] = []
        for issue in self._issues:
            key = (
                issue.code,
                issue.severity,
                issue.action,
                issue.safe_message,
                issue.anchor,
                canonical_json(issue.metadata),
            )
            if key not in first:
                first[key] = issue
                order.append(key)
            counts[key] += issue.count
        return tuple(
            first[key].model_copy(update={"count": counts[key]})
            for key in order
        )
