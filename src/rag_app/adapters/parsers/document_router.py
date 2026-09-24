"""候选多格式 Parser：保留 Word/PDF，显式接入文本与 docreader。"""

from __future__ import annotations

import io
import os
import stat
import time
import zipfile

from rag_app.adapters.parsers.format_config import enabled_upload_extensions
from rag_app.adapters.parsers.reading_view import build_reading_view_result
from rag_app.adapters.parsers.weknora_docreader import WeKnoraDocreaderClient
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.document_formats import (
    FORMAT_MEDIA_TYPES,
)
from rag_app.core.errors import InvalidDocument
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import ParseContext, ParseResult, ParseSource
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import ParserPort

_LOCAL_TEXT_EXTENSIONS = frozenset({".md", ".txt"})
_DOCR_EXTENSIONS = frozenset({".pptx", ".xlsx", ".csv"})
_UPSTREAM_SHA = "1edcd54b43606d9079bb36650efe3f68707a79ea"
_ENDPOINT_ENV = "RAG_WK_DOCREADER_ENDPOINT"
_TOKEN_ENV = "RAG_WK_DOCREADER_TOKEN"  # noqa: S105


def wrap_weknora_document_parser(fallback: ParserPort) -> ParserPort:
    """仅候选显式开启时包装稳定 Parser；默认原样返回。"""
    extras = enabled_upload_extensions() - {".docx"}
    if not extras:
        return fallback
    endpoint = os.environ.get(_ENDPOINT_ENV, "").strip()
    if extras & _DOCR_EXTENSIONS and not endpoint:
        raise ValueError(f"启用 PPTX/XLSX/CSV 前必须配置 {_ENDPOINT_ENV}。")
    return WeKnoraDocumentRouter(
        fallback,
        extensions=extras,
        endpoint=endpoint,
        auth_token=os.environ.get(_TOKEN_ENV) or None,
    )


class WeKnoraDocumentRouter:
    """对新增格式构造稳定来源的 DocumentIR。"""

    def __init__(
        self,
        fallback: ParserPort,
        *,
        extensions: frozenset[str],
        endpoint: str,
        auth_token: str | None = None,
    ) -> None:
        self._fallback = fallback
        self._extensions = extensions
        self._endpoint = endpoint
        self._client = (
            WeKnoraDocreaderClient(endpoint, auth_token=auth_token)
            if extensions & _DOCR_EXTENSIONS
            else None
        )

    @property
    def descriptor(self) -> ComponentDescriptor:
        """让上游提交、启用格式与端点身份进入索引配置。"""
        remote_formats = {FORMAT_MEDIA_TYPES[item] for item in self._extensions}
        identity = canonical_sha256(
            {
                "fallback": self._fallback.descriptor.model_dump(mode="json"),
                "upstream": _UPSTREAM_SHA,
                "extensions": sorted(self._extensions),
                "endpoint": self._endpoint,
                "adapter": "weknora-document-router-v1",
            }
        )
        return ComponentDescriptor(
            kind=ComponentKind.PARSER,
            name="weknora-document-router",
            version=f"1+{identity[-16:]}",
            mode=(
                ProviderMode.REMOTE
                if self._client is not None
                else ProviderMode.LOCAL
            ),
            capabilities=ComponentCapabilities(
                permits_network=self._client is not None,
                formats=tuple(
                    sorted(
                        set(self._fallback.descriptor.capabilities.formats)
                        | remote_formats
                    )
                ),
            ),
        )

    @property
    def parser_capabilities(self) -> ParserCapabilities:
        """声明新增文本能力，图片和表格仍为部分支持。"""
        fallback = self._fallback.parser_capabilities
        return ParserCapabilities(
            supported_extensions=tuple(
                sorted(set(fallback.supported_extensions) | self._extensions)
            ),
            supported_media_types=tuple(
                sorted(
                    set(fallback.supported_media_types)
                    | {FORMAT_MEDIA_TYPES[item] for item in self._extensions}
                )
            ),
            supports_tables="partial",
            supports_images="partial",
            supports_numbering="partial",
        )

    def probe_upload_extensions(self) -> frozenset[str]:
        """返回本地格式及 docreader 当前报告为可用的格式。"""
        available = {".docx"} | (self._extensions & _LOCAL_TEXT_EXTENSIONS)
        if self._client is None:
            return frozenset(available)
        engines = self._client.list_engines()
        for extension in self._extensions & _DOCR_EXTENSIONS:
            expected_engine = (
                "builtin" if extension == ".xlsx" else "markitdown"
            )
            if any(
                item.available
                and item.name == expected_engine
                and extension.lstrip(".") in item.file_types
                for item in engines
            ):
                available.add(extension)
        return frozenset(available)

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """对允许的新增格式调用本地文本或固定 docreader。"""
        extension = source.extension.casefold()
        if extension not in self._extensions:
            return self._fallback.parse(source, policy, context)
        if source.media_type.casefold() != FORMAT_MEDIA_TYPES[extension]:
            raise InvalidDocument(
                "扩展名与媒体类型不匹配。", stage="weknora-reading.input"
            )
        if context.cancel_check is not None:
            context.cancel_check()
        started_at = time.monotonic()
        if extension in _LOCAL_TEXT_EXTENSIONS:
            try:
                markdown = source.content.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise InvalidDocument(
                    "文本文件必须为有效 UTF-8。", stage="weknora-reading.input"
                ) from error
            if "\x00" in markdown:
                raise InvalidDocument(
                    "文本文件包含二进制内容。", stage="weknora-reading.input"
                )
            parser_id = "weknora-local-text"
            images: tuple[tuple[str, bytes], ...] = ()
        else:
            if self._client is None:
                raise RuntimeError("docreader 客户端未配置。")
            if extension in {".pptx", ".xlsx"}:
                _validate_office_package(
                    source.content, policy, expected_extension=extension
                )
            engine = "builtin" if extension == ".xlsx" else "markitdown"
            result = self._client.read_file(
                content=source.content,
                file_name=source.display_name,
                file_type=extension.lstrip("."),
                engine=engine,
            )
            markdown = result.markdown
            images = tuple(
                (image.mime_type, image.content) for image in result.images
            )
            parser_id = f"weknora-docreader-{engine}"
        if context.cancel_check is not None:
            context.cancel_check()
        parser_version = _UPSTREAM_SHA if extension in _DOCR_EXTENSIONS else "1"
        return build_reading_view_result(
            source,
            context,
            policy,
            markdown=markdown,
            parser_id=parser_id,
            parser_version=parser_version,
            images=images,
            started_at=started_at,
        )

    def close(self) -> None:
        """当前客户端逐次创建短连接，没有长期资源。"""
        return None


def _validate_office_package(
    content: bytes,
    policy: ParsingPolicy,
    *,
    expected_extension: str,
) -> None:
    """发往 docreader 前限制压缩包大小、路径和宏。"""
    if len(content) > policy.max_file_bytes:
        raise InvalidDocument(
            "Office 文件超过解析策略上限。", stage="weknora-reading.resource"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > policy.max_entries:
                raise ValueError("entry count")
            total = 0
            names: set[str] = set()
            for item in members:
                name = item.filename.replace("\\", "/")
                parts = name.split("/")
                if (
                    name.startswith("/")
                    or ":" in parts[0]
                    or ".." in parts
                    or any(part == "" for part in parts[:-1])
                    or name.casefold().endswith("vbaproject.bin")
                    or stat.S_IFMT(item.external_attr >> 16) == stat.S_IFLNK
                    or name in names
                ):
                    raise ValueError("unsafe member")
                names.add(name)
                total += item.file_size
                if (
                    item.file_size > policy.max_entry_bytes
                    or total > policy.max_uncompressed_bytes
                    or item.file_size
                    > max(1, item.compress_size) * policy.max_compression_ratio
                ):
                    raise ValueError("resource limit")
            main_parts = {
                ".pptx": "ppt/presentation.xml",
                ".xlsx": "xl/workbook.xml",
            }
            if (
                "[Content_Types].xml" not in names
                or main_parts[expected_extension] not in names
                or any(
                    part in names
                    for extension, part in main_parts.items()
                    if extension != expected_extension
                )
            ):
                raise ValueError("OOXML main part")
    except (ValueError, zipfile.BadZipFile) as error:
        raise InvalidDocument(
            "Office 包结构或资源上限不满足安全合同。",
            stage="weknora-reading.package",
        ) from error
