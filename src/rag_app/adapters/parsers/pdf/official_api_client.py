"""PaddleOCR 3.7.0 官方托管 API 的轻量同步 HTTP 客户端。

该模块复现官方 Python SDK 的提交、轮询和 JSONL 结果合同，但不导入
PaddleOCR、PaddleX 或任何本地推理依赖。应用始终只向官方托管 API
发送文件，并把返回结果转换为与官方 SDK 等价的只读对象。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx

_DEFAULT_BASE_URL = "https://paddleocr.aistudio-app.com"
_JOBS_PATH = "/api/v2/ocr/jobs"
_DOCUMENT_MODELS = frozenset(
    {
        "PaddleOCR-VL",
        "PaddleOCR-VL-1.5",
        "PaddleOCR-VL-1.6",
        "PP-StructureV3",
    }
)
_OCR_MODELS = frozenset({"PP-OCRv5", "PP-OCRv5-latin", "PP-OCRv6"})
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_LINES = 20_000
_MAX_ERROR_MESSAGE_LENGTH = 500
_HTTP_SUCCESS_MIN = 200
_HTTP_REDIRECT = 300
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_RATE_LIMITED = 429
_HTTP_SERVICE_UNAVAILABLE = 503
_HTTP_GATEWAY_TIMEOUT = 504


class PaddleOcrApiError(Exception):
    """PaddleOCR 官方 API 客户端错误基类。"""


class AuthError(PaddleOcrApiError):
    """Access Token 缺失、失效或无权限。"""


class InvalidRequestError(PaddleOcrApiError):
    """请求参数或输入文件不符合官方 API 合同。"""


class ApiError(PaddleOcrApiError):
    """官方 API 返回其他非成功状态。"""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class RateLimitError(ApiError):
    """官方 API 返回限流。"""

    def __init__(self, message: str) -> None:
        super().__init__(429, message)


class ServiceUnavailableError(ApiError):
    """官方 API 暂时不可用。"""


class JobFailedError(PaddleOcrApiError):
    """异步解析任务以失败状态结束。"""

    def __init__(self, job_id: str, error_message: str) -> None:
        self.job_id = job_id
        self.error_message = error_message
        super().__init__(f"Job {job_id} failed: {error_message}")


class RequestTimeoutError(PaddleOcrApiError):
    """单次 HTTP 请求超时。"""


class PollTimeoutError(PaddleOcrApiError):
    """轮询任务超过配置的总等待时间。"""

    def __init__(self, job_id: str, elapsed: float) -> None:
        self.job_id = job_id
        self.elapsed = elapsed
        super().__init__(f"Timed out waiting for job {job_id}")


class ResponseFormatError(PaddleOcrApiError):
    """官方 API 响应不符合公开合同。"""


class ResultParseError(PaddleOcrApiError):
    """结果 URL 返回的 JSONL 无法解析。"""


class NetworkError(PaddleOcrApiError):
    """官方 API 或结果存储连接失败。"""


@dataclass(frozen=True, slots=True)
class OfficialDocumentPage:
    """与官方 SDK `DocParsingPage` 等价的文档页结果。"""

    markdown_text: str
    markdown_images: Mapping[str, str] = field(default_factory=dict)
    output_images: Mapping[str, str] = field(default_factory=dict)
    pruned_result: object | None = None
    input_image_url: str | None = None
    exports: Mapping[str, object] = field(default_factory=dict)
    markdown: Mapping[str, object] = field(default_factory=dict)
    raw: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OfficialDocumentResult:
    """与官方 SDK `DocParsingResult` 等价的文档结果。"""

    job_id: str
    pages: tuple[OfficialDocumentPage, ...]
    data_info: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OfficialOcrPage:
    """与官方 SDK `OCRPage` 等价的单页 OCR 结果。"""

    pruned_result: object
    ocr_image_url: str | None = None
    doc_preprocessing_image_url: str | None = None
    input_image_url: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OfficialOcrResult:
    """与官方 SDK `OCRResult` 等价的 OCR 结果。"""

    job_id: str
    pages: tuple[OfficialOcrPage, ...]
    data_info: Mapping[str, object] = field(default_factory=dict)


class OfficialPaddleOcrApiClient:
    """只依赖 HTTP 的 PaddleOCR 官方同步客户端。"""

    def __init__(
        self,
        *,
        token: str,
        request_timeout: float = 300.0,
        poll_timeout: float = 600.0,
        base_url: str = _DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """创建官方托管 API 客户端。

        Args:
            token: 官方 API Access Token，仅用于任务接口鉴权。
            request_timeout: 每次 HTTP 请求的超时秒数。
            poll_timeout: 一个异步任务的最长轮询秒数。
            base_url: 官方 API 根地址；测试可注入等价端点。
            transport: 合同测试可注入的 HTTP Transport。

        Raises:
            AuthError: Token 为空。
            InvalidRequestError: 超时或 Base URL 无效。

        """
        if not token.strip():
            raise AuthError("Token is required.")
        if request_timeout <= 0 or poll_timeout <= 0:
            raise InvalidRequestError("Timeouts must be greater than zero.")
        normalized_base_url = base_url.rstrip("/")
        if not normalized_base_url:
            raise InvalidRequestError("Base URL is required.")
        self._token = token
        self._request_timeout = request_timeout
        self._poll_timeout = poll_timeout
        self._jobs_url = f"{normalized_base_url}{_JOBS_PATH}"
        self._client = httpx.Client(
            timeout=request_timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> OfficialPaddleOcrApiClient:
        """进入同步客户端作用域。"""
        return self

    def __exit__(self, *_args: object) -> None:
        """离开作用域时关闭 HTTP Client。"""
        self.close()

    def close(self) -> None:
        """关闭当前实例自行创建的 HTTP Client。"""
        self._client.close()

    def parse_document(
        self,
        *,
        file_path: str,
        model: str = "PaddleOCR-VL-1.6",
        options: object | None = None,
        page_ranges: str | None = None,
    ) -> OfficialDocumentResult:
        """提交 PDF 并同步等待完整文档解析结果。"""
        if model not in _DOCUMENT_MODELS:
            raise InvalidRequestError(
                f"Unsupported document parsing model: {model}"
            )
        job_id = self._submit_file(
            Path(file_path),
            model=model,
            options=options,
            page_ranges=page_ranges,
        )
        return _parse_document_result(job_id, self._poll(job_id))

    def ocr(
        self,
        *,
        file_path: str,
        model: str = "PP-OCRv6",
        options: object | None = None,
        page_ranges: str | None = None,
    ) -> OfficialOcrResult:
        """提交图片并同步等待 PP-OCR 结果。"""
        if model not in _OCR_MODELS:
            raise InvalidRequestError(f"Unsupported OCR model: {model}")
        job_id = self._submit_file(
            Path(file_path),
            model=model,
            options=options,
            page_ranges=page_ranges,
        )
        return _parse_ocr_result(job_id, self._poll(job_id))

    def _submit_file(
        self,
        path: Path,
        *,
        model: str,
        options: object | None,
        page_ranges: str | None,
    ) -> str:
        if not path.is_file():
            raise InvalidRequestError("Input file does not exist.")
        data = {
            "model": model,
            "optionalPayload": json.dumps(
                _options_payload(options),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        if page_ranges is not None:
            data["pageRanges"] = page_ranges
        media_type = (
            "application/pdf"
            if path.suffix.casefold() == ".pdf"
            else "image/png"
        )
        try:
            with path.open("rb") as source:
                response = self._client.post(
                    self._jobs_url,
                    headers=self._authorization_header,
                    data=data,
                    files={"file": (path.name, source, media_type)},
                    timeout=self._request_timeout,
                )
        except httpx.TimeoutException as error:
            raise RequestTimeoutError("Request timed out.") from error
        except httpx.RequestError as error:
            raise NetworkError("Connection failed.") from error
        payload = _response_data(response)
        job_id = payload.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise ResponseFormatError(
                "Response data must contain non-empty string 'jobId'."
            )
        return job_id

    def _poll(self, job_id: str) -> tuple[Mapping[str, object], ...]:
        interval = 3.0
        started = time.monotonic()
        deadline = started + self._poll_timeout
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise PollTimeoutError(job_id, now - started)
            data = self._job_status(job_id)
            state = data.get("state")
            if state not in {"pending", "running", "done", "failed"}:
                raise ResponseFormatError(
                    f"Unknown or missing job state: {state}"
                )
            if state == "done":
                return self._fetch_jsonl(_result_url(data))
            if state == "failed":
                message = data.get("errorMsg", "Unknown error")
                raise JobFailedError(job_id, _safe_message(message))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PollTimeoutError(job_id, time.monotonic() - started)
            time.sleep(min(interval, remaining))
            interval = min(interval * 1.5, 15.0)

    def _job_status(self, job_id: str) -> Mapping[str, object]:
        try:
            response = self._client.get(
                f"{self._jobs_url}/{job_id}",
                headers=self._authorization_header,
                timeout=self._request_timeout,
            )
        except httpx.TimeoutException as error:
            raise RequestTimeoutError("Request timed out.") from error
        except httpx.RequestError as error:
            raise NetworkError("Connection failed.") from error
        return _response_data(response)

    def _fetch_jsonl(self, url: str) -> tuple[Mapping[str, object], ...]:
        content = bytearray()
        try:
            with self._client.stream(
                "GET",
                url,
                timeout=self._request_timeout,
            ) as response:
                _raise_for_response(response)
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESULT_BYTES:
                        raise ResultParseError(
                            "Result JSONL exceeds the configured size limit."
                        )
        except httpx.TimeoutException as error:
            raise RequestTimeoutError("Result download timed out.") from error
        except httpx.RequestError as error:
            raise NetworkError("Result download failed.") from error
        try:
            text = bytes(content).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ResultParseError("Result JSONL is not UTF-8.") from error
        output: list[Mapping[str, object]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            if len(output) >= _MAX_RESULT_LINES:
                raise ResultParseError("Result JSONL contains too many lines.")
            try:
                value: object = json.loads(line)
            except json.JSONDecodeError as error:
                raise ResultParseError(
                    f"Malformed JSONL at line {line_number}."
                ) from error
            output.append(_mapping(value, "JSONL line must be an object."))
        return tuple(output)

    @property
    def _authorization_header(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


def _response_data(response: httpx.Response) -> Mapping[str, object]:
    _raise_for_response(response)
    try:
        value: object = response.json()
    except ValueError as error:
        raise ResponseFormatError("Response body is not valid JSON.") from error
    payload = _mapping(value, "Response body must be a JSON object.")
    if payload.get("code", 0) not in {0, None}:
        raise ApiError(response.status_code, _api_message(payload))
    return _mapping(
        payload.get("data"),
        "Response JSON must contain object field 'data'.",
    )


def _raise_for_response(response: httpx.Response) -> None:
    if _HTTP_SUCCESS_MIN <= response.status_code < _HTTP_REDIRECT:
        return
    message = _response_message(response)
    if response.status_code in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
        raise AuthError(f"Authentication failed: {message}")
    if response.status_code == _HTTP_BAD_REQUEST:
        raise InvalidRequestError(f"Bad request: {message}")
    if response.status_code == _HTTP_RATE_LIMITED:
        raise RateLimitError(f"Rate limit exceeded: {message}")
    if response.status_code in {
        _HTTP_SERVICE_UNAVAILABLE,
        _HTTP_GATEWAY_TIMEOUT,
    }:
        raise ServiceUnavailableError(response.status_code, message)
    raise ApiError(response.status_code, message)


def _response_message(response: httpx.Response) -> str:
    try:
        value: object = response.json()
    except ValueError:
        return _safe_message(response.text)
    if isinstance(value, Mapping):
        return _api_message(cast(Mapping[str, object], value))
    return _safe_message(response.text)


def _api_message(payload: Mapping[str, object]) -> str:
    for key in ("msg", "errorMsg", "message"):
        value = payload.get(key)
        if value:
            return _safe_message(value)
    data = payload.get("data")
    if isinstance(data, Mapping):
        nested = data.get("errorMsg")
        if nested:
            return _safe_message(nested)
    return "API request failed."


def _result_url(data: Mapping[str, object]) -> str:
    result = _mapping(
        data.get("resultUrl"),
        "Done job response must contain object 'resultUrl'.",
    )
    url = result.get("jsonUrl")
    if not isinstance(url, str) or not url:
        raise ResponseFormatError(
            "Done job resultUrl must contain non-empty string 'jsonUrl'."
        )
    return url


def _parse_document_result(
    job_id: str,
    jsonl_data: Sequence[Mapping[str, object]],
) -> OfficialDocumentResult:
    pages: list[OfficialDocumentPage] = []
    data_info: dict[str, object] = {}
    try:
        for line in jsonl_data:
            result = _mapping(line.get("result"), "Missing result object.")
            _merge_data_info(data_info, result.get("dataInfo"))
            raw_pages = _sequence(
                result.get("layoutParsingResults"),
                "Missing layoutParsingResults array.",
            )
            for raw_page in raw_pages:
                page = _mapping(raw_page, "Document page must be an object.")
                markdown = _mapping(
                    page.get("markdown"),
                    "Document page must contain markdown object.",
                )
                markdown_text = markdown.get("text")
                if not isinstance(markdown_text, str):
                    raise ResultParseError(
                        "Document page markdown.text must be a string."
                    )
                pages.append(
                    OfficialDocumentPage(
                        markdown_text=markdown_text,
                        markdown_images=_string_mapping(markdown.get("images")),
                        output_images=_string_mapping(page.get("outputImages")),
                        pruned_result=page.get("prunedResult"),
                        input_image_url=_optional_string(
                            page.get("inputImage")
                        ),
                        exports=_optional_mapping(page.get("exports")),
                        markdown=markdown,
                        raw=page,
                    )
                )
    except ResponseFormatError as error:
        raise ResultParseError(str(error)) from error
    return OfficialDocumentResult(
        job_id=job_id,
        pages=tuple(pages),
        data_info=data_info,
    )


def _parse_ocr_result(
    job_id: str,
    jsonl_data: Sequence[Mapping[str, object]],
) -> OfficialOcrResult:
    pages: list[OfficialOcrPage] = []
    data_info: dict[str, object] = {}
    try:
        for line in jsonl_data:
            result = _mapping(line.get("result"), "Missing result object.")
            _merge_data_info(data_info, result.get("dataInfo"))
            raw_pages = _sequence(
                result.get("ocrResults"),
                "Missing ocrResults array.",
            )
            for raw_page in raw_pages:
                page = _mapping(raw_page, "OCR page must be an object.")
                if "prunedResult" not in page:
                    raise ResultParseError(
                        "OCR page must contain prunedResult."
                    )
                pages.append(
                    OfficialOcrPage(
                        pruned_result=page["prunedResult"],
                        ocr_image_url=_optional_string(page.get("ocrImage")),
                        doc_preprocessing_image_url=_optional_string(
                            page.get("docPreprocessingImage")
                        ),
                        input_image_url=_optional_string(
                            page.get("inputImage")
                        ),
                        raw=page,
                    )
                )
    except ResponseFormatError as error:
        raise ResultParseError(str(error)) from error
    return OfficialOcrResult(
        job_id=job_id,
        pages=tuple(pages),
        data_info=data_info,
    )


def _options_payload(options: object | None) -> dict[str, object]:
    if options is None:
        return {}
    value = options
    to_payload = getattr(options, "to_payload", None)
    if callable(to_payload):
        value = to_payload()
    source = _mapping(value, "Options must be an object.")
    payload: dict[str, object] = {}
    for key, item in source.items():
        if key == "extra_options":
            payload.update(_optional_mapping(item))
        elif item is not None:
            payload[_snake_to_camel(key)] = item
    return payload


def _snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _merge_data_info(
    destination: dict[str, object], value: object | None
) -> None:
    if isinstance(value, Mapping):
        destination.update(
            {key: item for key, item in value.items() if isinstance(key, str)}
        )


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        return cast(Mapping[str, object], value)
    raise ResponseFormatError(message)


def _optional_mapping(value: object | None) -> Mapping[str, object]:
    if value is None:
        return {}
    return _mapping(value, "Optional value must be an object.")


def _string_mapping(value: object | None) -> Mapping[str, str]:
    mapping = _optional_mapping(value)
    if not all(isinstance(item, str) for item in mapping.values()):
        raise ResultParseError("Image mapping values must be strings.")
    return cast(Mapping[str, str], mapping)


def _sequence(value: object, message: str) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(Sequence[object], value)
    raise ResponseFormatError(message)


def _optional_string(value: object | None) -> str | None:
    return value if isinstance(value, str) else None


def _safe_message(value: object) -> str:
    return (
        str(value)
        .replace("\r", " ")
        .replace("\n", " ")[:_MAX_ERROR_MESSAGE_LENGTH]
    )


__all__ = [
    "AuthError",
    "InvalidRequestError",
    "JobFailedError",
    "NetworkError",
    "OfficialDocumentPage",
    "OfficialDocumentResult",
    "OfficialOcrPage",
    "OfficialOcrResult",
    "OfficialPaddleOcrApiClient",
    "PollTimeoutError",
    "RateLimitError",
    "RequestTimeoutError",
    "ResponseFormatError",
    "ResultParseError",
    "ServiceUnavailableError",
]
