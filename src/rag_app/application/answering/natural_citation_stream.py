"""逐增量解码私有引用标签，仅输出可见正文与受控引用编号。"""

from __future__ import annotations

import codecs
import re

from rag_app.application.answering.natural_answer import (
    CitationBinding,
    NaturalReference,
)
from rag_app.application.answering.natural_source_registry import (
    NaturalSourceRegistry,
)

_REF = re.compile(r'<ref id="(?P<handle>c[1-9][0-9]*)"/>')
_LEGACY = re.compile(r"\[S[^\]\r\n]{0,32}\]")
_RESERVED = (
    "<ref",
    "</ref",
    "<kb",
    "</kb",
    "<web",
    "</web",
    "<source",
    "</source",
    "<sources",
    "</sources",
)
_MAX_TAG_CHARS = 256


class NaturalCitationStream:
    """每轮独立使用的有界状态机，支持 UTF-8 字节或已解码文本。"""

    def __init__(self, registry: NaturalSourceRegistry) -> None:
        self._registry = registry
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        self._pending = ""
        self._discard_tag = False
        self._discard_legacy = False
        self._parts: list[str] = []
        self._cited: dict[str, None] = {}
        self._invalid: list[str] = []
        self._closed = False

    def feed(self, chunk: str | bytes) -> str:
        """只释放不可能再构成私有协议标签的可见文本。"""
        if self._closed:
            raise RuntimeError("引用流已关闭。")
        incoming = (
            self._utf8.decode(chunk, final=False)
            if isinstance(chunk, bytes)
            else chunk
        )
        visible = self._process(self._pending + incoming)
        self._parts.append(visible)
        return visible

    def flush(self) -> str:
        """正常 EOF 时丢弃残缺协议；取消时调用方无需释放缓冲。"""
        if self._closed:
            return ""
        self._closed = True
        tail = self._utf8.decode(b"", final=True)
        visible = self._process(self._pending + tail)
        if self._pending:
            if self._is_reserved_prefix(
                self._pending
            ) or self._pending.startswith("[S"):
                self._invalid.append("INCOMPLETE_CITATION")
            else:
                visible += self._pending
            self._pending = ""
        if self._discard_tag:
            self._invalid.append("INCOMPLETE_TAG")
            self._discard_tag = False
        if self._discard_legacy:
            self._invalid.append("INCOMPLETE_LEGACY")
            self._discard_legacy = False
        self._parts.append(visible)
        return visible

    @property
    def text(self) -> str:
        """解码后的完整公开正文，不从模型原文猜测引用。"""
        return "".join(self._parts)

    @property
    def binding(self) -> CitationBinding:
        """任何伪造或畸形标签均使本轮引用无效。"""
        if self._invalid:
            return CitationBinding(
                "invalid", invalid_markers=tuple(self._invalid)
            )
        if not self._cited:
            return CitationBinding("missing")
        references = self._registry.references(tuple(self._cited))
        return CitationBinding(
            "valid", cited_aliases=tuple(item.alias for item in references)
        )

    @property
    def cited_references(self) -> tuple[NaturalReference, ...]:
        """仅返回本轮 registry 授权且在模型输出中核验的证据。"""
        if self._invalid:
            return ()
        return self._registry.references(tuple(self._cited))

    def _process(self, data: str) -> str:  # noqa: PLR0912, PLR0915
        self._pending = ""
        output: list[str] = []
        while data:
            if self._discard_tag:
                end = data.find(">")
                if end < 0:
                    return "".join(output)
                data = data[end + 1 :]
                self._discard_tag = False
                continue
            if self._discard_legacy:
                end = data.find("]")
                if end < 0:
                    return "".join(output)
                data = data[end + 1 :]
                self._discard_legacy = False
                continue
            starts = tuple(
                index
                for marker in ("<", "[")
                if (index := data.find(marker)) >= 0
            )
            if not starts:
                output.append(data)
                break
            first = min(starts)
            output.append(data[:first])
            data = data[first:]
            if data.startswith("<"):
                lower = data.lower()
                if not self._is_reserved_prefix(lower):
                    output.append("<")
                    data = data[1:]
                    continue
                end = data.find(">")
                if end < 0:
                    if len(data) <= _MAX_TAG_CHARS:
                        self._pending = data
                    else:
                        self._invalid.append("OVERSIZED_TAG")
                        self._discard_tag = True
                    break
                tag = data[: end + 1]
                data = data[end + 1 :]
                if len(tag) > _MAX_TAG_CHARS:
                    self._invalid.append("OVERSIZED_TAG")
                else:
                    output.append(self._consume_tag(tag))
                continue
            if "[S".startswith(data):
                self._pending = data
                break
            if data.startswith("[S"):
                end = data.find("]")
                if end < 0:
                    if len(data) <= _MAX_TAG_CHARS:
                        self._pending = data
                    else:
                        self._invalid.append("OVERSIZED_LEGACY")
                        self._discard_legacy = True
                    break
                marker = data[: end + 1]
                data = data[end + 1 :]
                if _LEGACY.fullmatch(marker):
                    self._invalid.append("LEGACY_CITATION")
                else:
                    output.append(marker)
                continue
            output.append("[")
            data = data[1:]
        return "".join(output)

    def _consume_tag(self, tag: str) -> str:
        match = _REF.fullmatch(tag)
        if match is None:
            self._invalid.append("FORGED_OR_MALFORMED_TAG")
            return ""
        handle = match["handle"]
        reference = self._registry.resolve(handle)
        if reference is None:
            self._invalid.append("UNKNOWN_CITATION_HANDLE")
            return ""
        self._cited.setdefault(handle, None)
        return f"[{reference.alias}]"

    @staticmethod
    def _is_reserved_prefix(value: str) -> bool:
        """同时识别完整协议标签和可能被分块的前缀。"""
        return any(
            marker.startswith(value) or value.startswith(marker)
            for marker in _RESERVED
        )


__all__ = ["NaturalCitationStream"]
