"""PaddleOCR 双路径的 PP-OCRv6 有界区域复核。"""

from __future__ import annotations

import base64
import contextlib
import importlib
import io
import math
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlsplit

import httpx
from PIL import Image

from rag_app.adapters.parsers.pdf.official_api_client import (
    OfficialPaddleOcrApiClient,
)
from rag_app.core.models import ProviderCall

OfficialClientFactory = Callable[[str, float, float], object]
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_CROP_SIDE = 2048
_HTTP_REDIRECT = 300
_HTTP_RATE_LIMITED = 429
_TEXT_KEYS = frozenset(
    {"rectext", "rectexts", "rec_text", "rec_texts", "text", "texts"}
)


@dataclass(frozen=True, slots=True)
class CriticalOcrRead:
    """一次不携带图片或识别正文的脱敏调用结果。"""

    text: str | None
    call: ProviderCall
    reason_code: str


def render_pdf_region(
    content: bytes,
    *,
    page_index: int,
    page_width: float,
    page_height: float,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """按 Paddle 页面坐标渲染并裁剪一个 PDF 物理页区域。

    Args:
        content: 当前 document_version 的原始 PDF 字节。
        page_index: 零基物理页索引。
        page_width: Paddle 解析结果中的页面宽度。
        page_height: Paddle 解析结果中的页面高度。
        bbox: Paddle 解析块的左、上、右、下坐标。

    Returns:
        只包含目标块及小幅边距的 PNG 字节。

    Raises:
        ValueError: 渲染器不可用、页码越界或区域无效。

    """
    try:
        pdfium = importlib.import_module("pypdfium2")
    except ImportError as error:
        raise ValueError("PDF 区域渲染依赖未安装。") from error
    document: Any | None = None
    page: Any | None = None
    bitmap: Any | None = None
    try:
        document = pdfium.PdfDocument(content)
        if page_index < 0 or page_index >= len(document):
            raise ValueError("PDF 复核物理页超出当前版本。")
        page = document[page_index]
        bitmap = page.render(scale=3.0)
        image = bitmap.to_pil().copy()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("当前 PDF 物理页无法安全渲染。") from error
    finally:
        for resource in (bitmap, page, document):
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()
    left, top, right, bottom = bbox
    padding_x = max(2.0, (right - left) * 0.08)
    padding_y = max(2.0, (bottom - top) * 0.18)
    x_scale = image.width / page_width
    y_scale = image.height / page_height
    crop_box = (
        max(0, math.floor((left - padding_x) * x_scale)),
        max(0, math.floor((top - padding_y) * y_scale)),
        min(image.width, math.ceil((right + padding_x) * x_scale)),
        min(image.height, math.ceil((bottom + padding_y) * y_scale)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        raise ValueError("PDF 复核区域为空。")
    cropped = image.crop(crop_box)
    if max(cropped.size) > _MAX_CROP_SIDE:
        cropped.thumbnail(
            (_MAX_CROP_SIDE, _MAX_CROP_SIDE),
            Image.Resampling.LANCZOS,
        )
    output = io.BytesIO()
    cropped.save(output, format="PNG", optimize=True)
    return output.getvalue()


def recognized_contains(text: str, atom: str) -> bool:
    """保留单位大小写，同时忽略排版空白和等价全角符号。"""
    return _canonical_atom(atom) in _canonical_atom(text)


class PaddleSelfHostedCriticalOcr:
    """调用自托管完整 OCR 产线的 `/ocr` 图像接口。"""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._owned_client = client is None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            headers=headers,
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def request_payload(image: bytes) -> dict[str, object]:
        """返回可由合同测试断言的 PP-OCRv6 图像请求。"""
        return {
            "file": base64.b64encode(image).decode("ascii"),
            "fileType": 1,
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": True,
            "visualize": False,
        }

    def recognize(self, image: bytes) -> CriticalOcrRead:
        """执行一次不重试的有界区域 OCR。"""
        started = time.monotonic()
        try:
            response = self._client.post(
                f"{self._base_url}/ocr",
                json=self.request_payload(image),
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException:
            return self._result(
                started,
                None,
                "OCR_VERIFICATION_TIMEOUT",
                "timeout",
            )
        except httpx.RequestError:
            return self._result(
                started,
                None,
                "OCR_VERIFICATION_UNAVAILABLE",
                "unavailable",
            )
        if response.status_code >= _HTTP_REDIRECT:
            category = (
                "auth_or_model"
                if response.status_code in {401, 403}
                else "rate_limited"
                if response.status_code == _HTTP_RATE_LIMITED
                else "unavailable"
            )
            return self._result(
                started,
                None,
                "OCR_VERIFICATION_HTTP_FAILED",
                category,
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            return self._result(
                started,
                None,
                "OCR_VERIFICATION_RESPONSE_TOO_LARGE",
                "invalid_contract",
            )
        try:
            payload = response.json()
        except ValueError:
            return self._result(
                started,
                None,
                "OCR_VERIFICATION_RESPONSE_INVALID",
                "invalid_contract",
            )
        text = _self_hosted_text(payload)
        return self._result(
            started,
            text,
            (
                "OCR_VERIFICATION_COMPLETED"
                if text
                else "OCR_VERIFICATION_RESPONSE_INSUFFICIENT"
            ),
            "success" if text else "invalid_contract",
        )

    def close(self) -> None:
        """仅关闭当前 Adapter 自己创建的 HTTP Client。"""
        if self._owned_client:
            self._client.close()

    def _result(
        self,
        started: float,
        text: str | None,
        reason_code: str,
        category: str,
    ) -> CriticalOcrRead:
        endpoint = urlsplit(self._base_url).hostname
        return CriticalOcrRead(
            text=text,
            call=ProviderCall(
                provider_id=self._provider_id,
                operation="image.ocr.verify",
                call_count=1,
                retry_count=0,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                reason_code=reason_code,
                model="PP-OCRv6",
                endpoint=endpoint,
                attempt_count=1,
                status_category=category,
                rate_limited=category == "rate_limited",
                input_count=1,
            ),
            reason_code=reason_code,
        )


class PaddleOfficialCriticalOcr:
    """通过 PaddleOCR 3.7.0 官方托管 API 调用 PP-OCRv6。"""

    def __init__(
        self,
        *,
        provider_id: str,
        access_token: str,
        request_timeout_seconds: float,
        poll_timeout_seconds: float,
        client_factory: OfficialClientFactory | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._access_token = access_token
        self._request_timeout = request_timeout_seconds
        self._poll_timeout = poll_timeout_seconds
        self._client_factory = client_factory or _official_client

    def recognize(self, image: bytes) -> CriticalOcrRead:
        """使用官方 API 对一个 PNG 区域执行一次同步 OCR。"""
        started = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile(suffix=".png") as temporary:
                temporary.write(image)
                temporary.flush()
                with self._client() as client:
                    method = getattr(client, "ocr", None)
                    if not callable(method):
                        return self._result(
                            started,
                            None,
                            "OCR_VERIFICATION_CLIENT_INVALID",
                            "invalid_contract",
                        )
                    result = method(
                        file_path=str(Path(temporary.name)),
                        model=_official_ocr_model(),
                        options=_official_ocr_options(),
                    )
        except Exception as error:
            name = type(error).__name__
            category = (
                "auth_or_model"
                if name in {"AuthError", "InvalidRequestError"}
                else "rate_limited"
                if name == "RateLimitError"
                else "unavailable"
            )
            return self._result(
                started,
                None,
                f"OCR_VERIFICATION_{_safe_error_name(name)}",
                category,
            )
        text = _official_text(result)
        return self._result(
            started,
            text,
            (
                "OCR_VERIFICATION_COMPLETED"
                if text
                else "OCR_VERIFICATION_RESPONSE_INSUFFICIENT"
            ),
            "success" if text else "invalid_contract",
            provider_request_id=_string_value(result, "job_id", "jobId"),
        )

    def close(self) -> None:
        """官方 HTTP Client 已按单次调用关闭。"""

    @contextlib.contextmanager
    def _client(self) -> Iterator[object]:
        client = self._client_factory(
            self._access_token,
            self._request_timeout,
            self._poll_timeout,
        )
        entered = getattr(client, "__enter__", None)
        exited = getattr(client, "__exit__", None)
        if callable(entered) and callable(exited):
            scoped = entered()
            try:
                yield scoped
            finally:
                exited(None, None, None)
            return
        try:
            yield client
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()

    def _result(
        self,
        started: float,
        text: str | None,
        reason_code: str,
        category: str,
        *,
        provider_request_id: str | None = None,
    ) -> CriticalOcrRead:
        diagnostics = (
            ()
            if provider_request_id is None
            else (("provider_request_id", provider_request_id),)
        )
        return CriticalOcrRead(
            text=text,
            call=ProviderCall(
                provider_id=self._provider_id,
                operation="image.ocr.verify",
                call_count=1,
                retry_count=0,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                reason_code=reason_code,
                model="PP-OCRv6",
                endpoint="official_api",
                attempt_count=1,
                status_category=category,
                rate_limited=category == "rate_limited",
                input_count=1,
                transport_diagnostics=diagnostics,
            ),
            reason_code=reason_code,
        )


def _self_hosted_text(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    if payload.get("errorCode", payload.get("error_code", 0)) not in {
        None,
        0,
        "0",
    }:
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    pages = result.get("ocrResults")
    if not _is_sequence(pages) or len(pages) != 1:
        return None
    return _recognized_text(pages[0])


def _official_text(result: object) -> str | None:
    pages = getattr(result, "pages", None)
    if not _is_sequence(pages) or len(pages) != 1:
        return None
    return _recognized_text(pages[0])


def _recognized_text(value: object) -> str | None:
    texts: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).replace("_", "").casefold()
                if normalized in _TEXT_KEYS:
                    texts.extend(_flatten_strings(nested))
                elif isinstance(nested, (Mapping, list, tuple)):
                    visit(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
            return
        pruned = getattr(item, "pruned_result", None)
        if pruned is not None:
            visit(pruned)

    visit(value)
    joined = "\n".join(text for text in texts if text.strip())
    return joined or None


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if _is_sequence(value):
        return [item for item in value if isinstance(item, str)]
    return []


def _canonical_atom(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("−", "-").replace("–", "-")
    return "".join(normalized.split())


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _string_value(value: object, *names: str) -> str | None:
    for name in names:
        candidate = (
            value.get(name)
            if isinstance(value, Mapping)
            else getattr(value, name, None)
        )
        if isinstance(candidate, str) and candidate:
            return candidate[:200]
    return None


def _safe_error_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "_" for character in value
    ).upper()
    return cleaned[:60] or "FAILED"


def _official_ocr_model() -> str:
    return "PP-OCRv6"


def _official_client(
    token: str, request_timeout: float, poll_timeout: float
) -> object:
    return OfficialPaddleOcrApiClient(
        token=token,
        request_timeout=request_timeout,
        poll_timeout=poll_timeout,
    )


def _official_ocr_options() -> dict[str, object]:
    return {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "visualize": False,
    }


__all__ = [
    "CriticalOcrRead",
    "PaddleOfficialCriticalOcr",
    "PaddleSelfHostedCriticalOcr",
    "recognized_contains",
    "render_pdf_region",
]
