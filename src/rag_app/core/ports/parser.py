"""格式中立同步 Parser 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models import ParsePolicy, ParseResult, ParseSource


class ParserPort(Protocol):
    """同步、无隐式网络且同输入幂等的解析端口。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回不含 secret 的组件身份。

        Args:
            无参数；读取当前 parser。

        Returns:
            可审计组件描述符。

        """
        ...

    def parse(self, source: ParseSource, policy: ParsePolicy) -> ParseResult:
        """解析一个受控字节源。

        Args:
            source: 字节内容和格式元数据。
            policy: 冻结且格式中立的解析策略。

        Returns:
            Document IR 与解析报告。

        """
        ...
