"""格式中立同步 Parser 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor, ParserCapabilities
from rag_app.core.models import ParseContext, ParseResult, ParseSource
from rag_app.core.policies import ParsingPolicy


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

    @property
    def parser_capabilities(self) -> ParserCapabilities:
        """返回格式中立 Parser 能力。

        Args:
            无参数；读取当前 parser。

        Returns:
            不夸大复杂结构支持程度的能力声明。

        """
        ...

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """解析一个受控字节源。

        Args:
            source: 字节内容和格式元数据。
            policy: 冻结且格式中立的解析策略。
            context: 不进入策略指纹的逻辑文档身份。

        Returns:
            Document IR 与解析报告。

        """
        ...
