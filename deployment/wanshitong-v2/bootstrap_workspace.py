"""初始化独立 8289 原生测试工作空间，不连接生产或复制员工账号。

在测试引擎网络中运行，先临时允许注册一个服务账号，完成后再关闭注册并
开放测试入口。本脚本只接受 http://app:8080，不输出 Token 或 Secret。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

_EMAIL = "wxd-admin@internal.test"
_USERNAME = "wxd-service-admin"
_PUBLIC_KB_NAME = "湾小度公共资料"
_KEY_NAME = "wxd-v2-gateway"
_EMBEDDING_MODEL = "wkab-qwen3-embedding-0-6b"
_SUMMARY_MODEL = "wkab-qwen3-8b-awq"
# message_history 是上游原生历史知识库能力名；不存在名为 history 的能力。
_CAPABILITIES = ("chat", "retrieve", "message_history")
_SECRET_UID = 10001
_SECRET_GID = 10001
_TIMEOUT_SECONDS = 60
_TEST_ENGINE_PORT = 8080


class BootstrapError(RuntimeError):
    """可直接输出的有界初始化错误，禁止包含上游正文或凭据。"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(  # noqa: PLR0913, PLR0917 - 继承标准库回调签名。
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


class TestEngine:
    """只向独立测试网络的固定 app 服务发起 JSON 请求。"""

    def __init__(self, base_url: str) -> None:
        """拒绝生产地址、任意重定向与系统代理。"""
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "app"
            or parsed.port != _TEST_ENGINE_PORT
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise BootstrapError("仅允许独立测试引擎地址 http://app:8080。")
        self._base_url = "http://app:8080"
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.token: str | None = None
        self.tenant_id: int | None = None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        allow_unauthorized: bool = False,
    ) -> dict[str, Any] | None:
        """解析原生固定接口响应，错误只报告路径和 HTTP 状态。"""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.tenant_id:
            headers["X-Tenant-ID"] = str(self.tenant_id)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(  # noqa: S310 - 固定HTTP测试服务，禁止重定向。
            self._base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(
                request, timeout=_TIMEOUT_SECONDS
            ) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            status_code = error.code
            error.close()
            if allow_unauthorized and status_code == HTTPStatus.UNAUTHORIZED:
                return None
            raise BootstrapError(
                f"测试接口 {method} {path} 返回 HTTP {status_code}。"
            ) from None
        except URLError:
            raise BootstrapError("无法连接独立测试引擎。") from None
        except ValueError:
            raise BootstrapError(f"测试接口 {path} 未返回有效 JSON。") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise BootstrapError(f"测试接口 {path} 未成功完成。")
        return payload

    def object(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """读取原生 data 对象。"""
        result = self.request(method, path, body)
        data = result.get("data") if result else None
        if not isinstance(data, dict):
            raise BootstrapError(f"测试接口 {path} 缺少 data 对象。")
        return data

    def rows(self, path: str) -> list[dict[str, Any]]:
        """读取原生 data 列表。"""
        result = self.request("GET", path)
        data = result.get("data") if result else None
        if not isinstance(data, list) or any(
            not isinstance(row, dict) for row in data
        ):
            raise BootstrapError(f"测试接口 {path} 缺少 data 列表。")
        return data


def _read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"测试密钥文件不存在或不是普通文件：{path.name}。")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise BootstrapError(f"测试密钥文件为空：{path.name}。")
    _private_permissions(path)
    return value


def _private_permissions(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if os.geteuid() == 0:
        os.chown(path, _SECRET_UID, _SECRET_GID)
    elif path.stat().st_uid != _SECRET_UID:
        raise BootstrapError("测试密钥必须由 UID 10001 持有。")


def _write_secret(path: Path, value: str) -> None:
    if path.is_symlink():
        raise BootstrapError("测试密钥目标不能为符号链接。")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value + "\n")
    _private_permissions(path)


def _login(engine: TestEngine, password: str) -> None:
    body = {"email": _EMAIL, "password": password}
    result = engine.request(
        "POST", "/api/v1/auth/login", body, allow_unauthorized=True
    )
    if result is None:
        engine.request(
            "POST",
            "/api/v1/auth/register",
            {"username": _USERNAME, **body},
        )
        result = engine.request("POST", "/api/v1/auth/login", body)
    token = result.get("token") if result else None
    if not isinstance(token, str) or not token:
        raise BootstrapError("测试服务账号登录未返回 Token。")
    engine.token = token


def _models(engine: TestEngine) -> None:
    models = engine.rows("/api/v1/models")
    present = {row.get("id") for row in models}
    missing = sorted({_EMBEDDING_MODEL, _SUMMARY_MODEL} - present)
    if missing:
        raise BootstrapError("测试引擎缺少声明式模型 ID：" + ", ".join(missing))


def _public_kb(engine: TestEngine, storage_id: str | None) -> str:
    existing = [
        row
        for row in engine.rows("/api/v1/knowledge-bases")
        if row.get("name") == _PUBLIC_KB_NAME
    ]
    if len(existing) > 1:
        raise BootstrapError("测试空间有多个同名公共知识库，请先核对。")
    if existing:
        item = existing[0]
        if (
            item.get("embedding_model_id") != _EMBEDDING_MODEL
            or item.get("summary_model_id") != _SUMMARY_MODEL
            or item.get("type") != "document"
        ):
            raise BootstrapError("已有测试公共知识库的模型或类型与配置不符。")
    else:
        body: dict[str, Any] = {
            "name": _PUBLIC_KB_NAME,
            "description": "8289测试公共资料，与8288生产存储隔离。",
            "type": "document",
            "embedding_model_id": _EMBEDDING_MODEL,
            "summary_model_id": _SUMMARY_MODEL,
            "image_processing_config": {"model_id": "wxd-paddleocr-vl"},
            "vlm_config": {"enabled": True, "model_id": "wxd-paddleocr-vl"},
            "chunking_config": {
                "chunk_size": 512,
                "chunk_overlap": 80,
                "separators": ["\n\n", "\n", "。", "！", "？", ";"],
            },
            "question_generation_config": {
                "enabled": False,
                "question_count": 3,
            },
        }
        if storage_id:
            body["storage_backend_id"] = storage_id
        item = engine.object("POST", "/api/v1/knowledge-bases", body)
    kb_id = item.get("id")
    if not isinstance(kb_id, str) or not kb_id:
        raise BootstrapError("测试公共知识库未返回 ID。")
    return kb_id


def _workspace_key(engine: TestEngine, kb_id: str) -> str:
    path = f"/api/v1/tenants/{engine.tenant_id}/api-keys"
    existing = [
        row for row in engine.rows(path) if row.get("name") == _KEY_NAME
    ]
    if len(existing) > 1:
        raise BootstrapError("测试空间有多个同名服务 Key，请先核对。")
    if existing:
        row = existing[0]
        if (
            row.get("full_access") is not False
            or kb_id not in row.get("knowledge_base_ids", [])
            or not set(_CAPABILITIES).issubset(row.get("capabilities", []))
        ):
            raise BootstrapError("已有测试服务 Key 的范围或能力与配置不符。")
        value = row.get("api_key")
    else:
        row = engine.object(
            "POST",
            path,
            {
                "name": _KEY_NAME,
                "full_access": False,
                "knowledge_base_ids": [kb_id],
                "capabilities": list(_CAPABILITIES),
            },
        )
        value = row.get("token") or row.get("api_key")
    if not isinstance(value, str) or not value:
        raise BootstrapError("测试服务 Key 未返回可保存的值。")
    return value


def bootstrap(
    base_url: str,
    secrets_dir: Path,
    output_path: Path,
) -> dict[str, str | int | None]:
    """创建或复用唯一服务账号、公共库和限定范围 Key。"""
    engine = TestEngine(base_url)
    password = _read_secret(secrets_dir / "weknora_admin_password")
    signing_key = _read_secret(secrets_dir / "external_signing_key")
    _login(engine, password)
    identity = engine.object("GET", "/api/v1/auth/me")
    tenant = identity.get("tenant")
    user = identity.get("user")
    if not isinstance(tenant, dict) or not isinstance(tenant.get("id"), int):
        raise BootstrapError("测试服务账号未获得工作空间。")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise BootstrapError("测试服务账号未返回用户 ID。")
    engine.tenant_id = tenant["id"]
    _models(engine)
    storage = engine.request("GET", "/api/v1/storage-backends")
    storage_id = storage.get("default_storage_backend_id") if storage else None
    if storage_id is not None and not isinstance(storage_id, str):
        raise BootstrapError("测试工作空间的默认存储 ID 无效。")
    kb_id = _public_kb(engine, storage_id)
    key = _workspace_key(engine, kb_id)
    engine.request(
        "PUT",
        f"/api/v1/tenants/{engine.tenant_id}/api-principal-config",
        {
            "mode": "signed_token",
            "direct_header_name": "X-External-User-ID",
            "signed_token_header_name": "X-External-User-Token",
            "require_direct_header": False,
            "hmac_secret": signing_key,
        },
    )
    _write_secret(secrets_dir / "workspace_api_key", key)
    summary: dict[str, str | int | None] = {
        "status": "ready",
        "tenant_id": engine.tenant_id,
        "public_kb_id": kb_id,
        "native_admin_user_id": user["id"],
        "default_storage_backend_id": storage_id,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    """解析测试初始化参数，标准输出只包含非秘密 ID 和状态。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://app:8080")
    parser.add_argument(
        "--secrets-dir", type=Path, default=Path("/run/wst-secrets")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/release/bootstrap.json")
    )
    args = parser.parse_args()
    try:
        summary = bootstrap(args.base_url, args.secrets_dir, args.output)
    except (BootstrapError, OSError, ValueError) as error:
        # 仅 BootstrapError 的消息经过有界处理，其他异常不输出路径/原生正文。
        message = (
            str(error)
            if isinstance(error, BootstrapError)
            else "测试初始化文件或配置处理失败。"
        )
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
