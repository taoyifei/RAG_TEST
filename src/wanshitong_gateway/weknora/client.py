"""固定路由的 WeKnora HTTP 客户端，不执行旧问答服务。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=10.0)
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_BAD_GATEWAY = 502


class NativeHttpError(Exception):
    """原生 API 的有界错误，不携带认证请求头。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class WeKnoraClient:
    """只向服务端固定原生地址发送有限 API 调用。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        signer: ExternalPrincipalSigner,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("原生 API 地址无效。")
        if not api_key:
            raise ValueError("必须配置受限原生 API Key。")
        self._api_key = api_key
        self._signer = signer
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        """关闭连接池。"""
        await self._http.aclose()

    def _headers(self, user_id: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
            "X-External-User-Token": self._signer.sign(user_id),
        }

    async def create_session(self, user_id: str) -> str:
        """为已认证用户创建原生会话。"""
        response = await self._http.post(
            "/api/v1/sessions",
            json={},
            headers=self._headers(user_id),
        )
        payload = _require_success(response)
        data = payload.get("data")
        session_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(session_id, str):
            raise NativeHttpError(502, "原生会话响应缺少 ID。")
        return session_id

    async def knowledge_chat(
        self,
        *,
        user_id: str,
        session_id: str,
        question: str,
        knowledge_base_ids: tuple[str, ...],
    ) -> AsyncIterator[bytes]:
        """逐字节转发一次原生自然问答，绝不重试生成 POST。"""
        if not knowledge_base_ids:
            raise ValueError("普通问答知识库范围不能为空。")
        async with self._http.stream(
            "POST",
            f"/api/v1/knowledge-chat/{session_id}",
            json={
                "query": question,
                "knowledge_base_ids": list(knowledge_base_ids),
            },
            headers={
                **self._headers(user_id),
                "Accept": "text/event-stream",
            },
        ) as response:
            if response.status_code != _HTTP_OK:
                _require_success(await response.aread(), response.status_code)
                raise NativeHttpError(
                    response.status_code, "原生问答请求失败。"
                )
            async for chunk in response.aiter_bytes():
                yield chunk

    async def stop(
        self, *, user_id: str, session_id: str, message_id: str
    ) -> bool:
        """使用原生助手消息 ID 停止实际生成。"""
        response = await self._http.post(
            f"/api/v1/sessions/{session_id}/stop",
            json={"message_id": message_id},
            headers=self._headers(user_id),
        )
        payload = _require_success(response)
        return payload.get("success") is True

    async def messages(
        self, *, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """读取属于外部主体的原生历史。"""
        response = await self._http.get(
            f"/api/v1/messages/{session_id}/load",
            headers=self._headers(user_id),
        )
        payload = _require_success(response)
        data = payload.get("data")
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            raise NativeHttpError(502, "原生历史响应格式无效。")
        return data

    async def session(
        self, *, user_id: str, session_id: str
    ) -> dict[str, Any]:
        """读取原生会话标题及所属身份。"""
        response = await self._http.get(
            f"/api/v1/sessions/{session_id}",
            headers=self._headers(user_id),
        )
        payload = _require_success(response)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NativeHttpError(502, "原生会话响应格式无效。")
        return data

    async def message_resource(
        self,
        *,
        user_id: str,
        session_id: str,
        message_id: str,
        file_path: str,
    ) -> httpx.Response:
        """通过原生消息级资源鉴权读取已绑定文件。"""
        response = await self._http.get(
            f"/api/v1/sessions/{session_id}/messages/{message_id}/files",
            params={"file_path": file_path},
            headers=self._headers(user_id),
        )
        if response.status_code != _HTTP_OK:
            _require_success(response)
            raise NativeHttpError(
                response.status_code, "原生引用资源不可用。"
            )
        return response


def _require_success(
    response: httpx.Response | bytes, status_code: int | None = None
) -> dict[str, Any]:
    if isinstance(response, httpx.Response):
        status_code = response.status_code
        body = response.content
    else:
        body = response
    if status_code is None:
        raise ValueError("原生 API 响应缺少状态码。")
    try:
        payload = httpx.Response(status_code, content=body).json()
    except ValueError as error:
        raise NativeHttpError(status_code, "原生 API 返回非 JSON。") from error
    if not isinstance(payload, dict):
        raise NativeHttpError(status_code, "原生 API 响应格式无效。")
    if status_code >= _HTTP_BAD_REQUEST or payload.get("success") is False:
        # 原生错误可能含内部路径与配置，仅向调用方返回通用描述。
        error_status = (
            status_code
            if status_code >= _HTTP_BAD_REQUEST
            else _HTTP_BAD_GATEWAY
        )
        raise NativeHttpError(
            error_status,
            "原生 API 请求失败。",
        )
    return payload
