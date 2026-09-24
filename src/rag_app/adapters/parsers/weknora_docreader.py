"""固定 WeKnora docreader gRPC 协议的有界客户端。"""

from __future__ import annotations

from dataclasses import dataclass

import grpc  # type: ignore[import-untyped]

from rag_app.adapters.parsers.weknora_proto import (
    docreader_pb2,
    docreader_pb2_grpc,
)
from rag_app.core.errors import (
    InvalidDocument,
    ProviderInvalidResponse,
    ProviderUnavailable,
)

_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_MARKDOWN_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_COUNT = 256
_MAX_GRPC_MESSAGE_BYTES = 40 * 1024 * 1024
_UPSTREAM_SHA = "1edcd54b43606d9079bb36650efe3f68707a79ea"


@dataclass(frozen=True, slots=True)
class DocreaderImage:
    """仅接受上游回传的 inline 图片字节。"""

    original_ref: str
    mime_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class DocreaderResult:
    """完整成功的解析响应；错误或中断时不返回半成品。"""

    markdown: str
    images: tuple[DocreaderImage, ...]
    metadata: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DocreaderEngine:
    """由固定版本服务报告的引擎与格式能力。"""

    name: str
    file_types: frozenset[str]
    available: bool


class WeKnoraDocreaderClient:
    """优先 ReadStream，仅在 UNIMPLEMENTED 时回退 unary Read。"""

    upstream_sha = _UPSTREAM_SHA

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 30.0,
        auth_token: str | None = None,
    ) -> None:
        if not endpoint or ":" not in endpoint or timeout_seconds <= 0:
            raise ValueError(
                "docreader endpoint 必须为 host:port 且超时为正数。"
            )
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._auth_token = auth_token

    def list_engines(self) -> tuple[DocreaderEngine, ...]:
        """查询真实服务的引擎登记；网络失败时不把配置当能力。"""
        metadata = (
            ()
            if self._auth_token is None
            else (("authorization", f"Bearer {self._auth_token}"),)
        )
        with grpc.insecure_channel(
            self._endpoint,
            options=(
                ("grpc.max_receive_message_length", _MAX_GRPC_MESSAGE_BYTES),
            ),
        ) as channel:
            stub = docreader_pb2_grpc.DocReaderStub(channel)  # type: ignore[no-untyped-call]
            try:
                response = stub.ListEngines(
                    docreader_pb2.ListEnginesRequest(),
                    timeout=min(self._timeout_seconds, 3.0),
                    metadata=metadata,
                )
            except grpc.RpcError as error:
                raise ProviderUnavailable(
                    "docreader 引擎探测失败。",
                    stage="weknora-docreader.probe",
                ) from error
        if not response.engines:
            raise ProviderInvalidResponse(
                "docreader 未报告任何解析引擎。",
                stage="weknora-docreader.probe",
            )
        return tuple(
            DocreaderEngine(
                name=engine.name,
                file_types=frozenset(engine.file_types),
                available=engine.available,
            )
            for engine in response.engines
        )

    def read_file(
        self,
        *,
        content: bytes,
        file_name: str,
        file_type: str,
        engine: str,
        overrides: dict[str, str] | None = None,
    ) -> DocreaderResult:
        """读取单文件；不传 URL，也不对未知失败隐式换引擎。"""
        if not content or len(content) > _MAX_FILE_BYTES:
            raise InvalidDocument(
                "文件为空或超过 docreader 32 MiB 输入上限。",
                stage="weknora-docreader.input",
            )
        if engine not in {"builtin", "markitdown"}:
            raise ValueError("docreader 引擎必须显式在允许列表中。")
        config = docreader_pb2.ReadConfig(
            parser_engine=engine,
            parser_engine_overrides=overrides or {},
        )
        request = docreader_pb2.ReadRequest(
            file_content=content,
            file_name=file_name,
            file_type=file_type,
            config=config,
        )
        options = (
            ("grpc.max_receive_message_length", _MAX_GRPC_MESSAGE_BYTES),
            ("grpc.max_send_message_length", _MAX_GRPC_MESSAGE_BYTES),
        )
        metadata = (
            ()
            if self._auth_token is None
            else (("authorization", f"Bearer {self._auth_token}"),)
        )
        with grpc.insecure_channel(self._endpoint, options=options) as channel:
            stub = docreader_pb2_grpc.DocReaderStub(channel)  # type: ignore[no-untyped-call]
            try:
                return self._read_stream(stub, request, metadata)
            except grpc.RpcError as error:
                if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                    raise ProviderUnavailable(
                        "docreader gRPC 调用失败。",
                        stage="weknora-docreader.transport",
                    ) from error
                try:
                    response = stub.Read(
                        request,
                        timeout=self._timeout_seconds,
                        metadata=metadata,
                    )
                except grpc.RpcError as unary_error:
                    raise ProviderUnavailable(
                        "docreader unary 回退调用失败。",
                        stage="weknora-docreader.transport",
                    ) from unary_error
                return self._from_unary(response)

    def _read_stream(
        self,
        stub: docreader_pb2_grpc.DocReaderStub,
        request: docreader_pb2.ReadRequest,
        metadata: tuple[tuple[str, str], ...],
    ) -> DocreaderResult:
        call = stub.ReadStream(
            request, timeout=self._timeout_seconds, metadata=metadata
        )
        meta: docreader_pb2.ReadStreamMeta | None = None
        images: list[DocreaderImage] = []
        total_image_bytes = 0
        for frame in call:
            kind = frame.WhichOneof("payload")
            if meta is None:
                if kind != "meta":
                    raise ProviderInvalidResponse(
                        "docreader 首帧不是 meta。",
                        stage="weknora-docreader.response",
                    )
                meta = frame.meta
                self._check_meta(meta)
                continue
            if kind != "image":
                raise ProviderInvalidResponse(
                    "docreader 图片流帧顺序或类型错误。",
                    stage="weknora-docreader.response",
                )
            image = self._image(frame.image)
            total_image_bytes += len(image.content)
            if (
                len(images) >= _MAX_IMAGE_COUNT
                or total_image_bytes > _MAX_TOTAL_IMAGE_BYTES
            ):
                raise ProviderInvalidResponse(
                    "docreader 图片总量超过上限。",
                    stage="weknora-docreader.response",
                )
            images.append(image)
        if meta is None:
            raise ProviderInvalidResponse(
                "docreader 未返回 meta。",
                stage="weknora-docreader.response",
            )
        count = meta.image_count
        if count and count != len(images):
            raise ProviderInvalidResponse(
                "docreader 图片流不完整。",
                stage="weknora-docreader.response",
            )
        return DocreaderResult(
            markdown=meta.markdown_content,
            images=tuple(images),
            metadata=tuple(sorted(meta.metadata.items())),
        )

    def _from_unary(
        self, response: docreader_pb2.ReadResponse
    ) -> DocreaderResult:
        self._check_meta(response)
        images = tuple(self._image(item) for item in response.image_refs)
        if (
            len(images) > _MAX_IMAGE_COUNT
            or sum(len(item.content) for item in images)
            > _MAX_TOTAL_IMAGE_BYTES
        ):
            raise ProviderInvalidResponse(
                "docreader 图片总量超过上限。",
                stage="weknora-docreader.response",
            )
        return DocreaderResult(
            markdown=response.markdown_content,
            images=images,
            metadata=tuple(sorted(response.metadata.items())),
        )

    @staticmethod
    def _check_meta(
        meta: docreader_pb2.ReadStreamMeta | docreader_pb2.ReadResponse,
    ) -> None:
        if meta.error:
            raise InvalidDocument(
                "docreader 解析失败。", stage="weknora-docreader.parse"
            )
        markdown = meta.markdown_content
        if not markdown.strip() or (
            len(markdown.encode("utf-8")) > _MAX_MARKDOWN_BYTES
        ):
            raise ProviderInvalidResponse(
                "docreader 返回空正文或正文超过上限。",
                stage="weknora-docreader.response",
            )

    @staticmethod
    def _image(image: docreader_pb2.ImageRef) -> DocreaderImage:
        content = image.image_data
        mime_type = image.mime_type
        if (
            not content
            or len(content) > _MAX_IMAGE_BYTES
            or mime_type
            not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "image/bmp",
            }
        ):
            raise ProviderInvalidResponse(
                "docreader 图片缺少 inline 字节或类型不受支持。",
                stage="weknora-docreader.response",
            )
        return DocreaderImage(
            original_ref=image.original_ref,
            mime_type=mime_type,
            content=content,
        )
