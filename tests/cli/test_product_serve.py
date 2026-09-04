"""真实 CLI 子进程的 Product Runtime 默认入口回归。"""

from __future__ import annotations

import json
import multiprocessing
import socket
import time
from pathlib import Path
from typing import cast

import httpx
import pytest

from rag_app.cli import main


def _serve_product() -> None:
    raise SystemExit(main(["serve"]))


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _get_json(client: httpx.Client, path: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads(client.get(path).content))


def test_default_serve_starts_product_runtime_in_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>知识库工作台</title>", encoding="utf-8"
    )
    bootstrap = tmp_path / "bootstrap"
    bootstrap_value = "-".join(("subprocess", "bootstrap", "credential"))
    bootstrap.write_text(bootstrap_value, encoding="utf-8")
    bootstrap.chmod(0o600)
    port = _free_loopback_port()
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RAG_FRONTEND_DIR", str(frontend))
    monkeypatch.setenv("RAG_ADMIN_BOOTSTRAP_TOKEN_FILE", str(bootstrap))
    monkeypatch.setenv("RAG_HOST", "127.0.0.1")
    monkeypatch.setenv("RAG_PORT", str(port))
    monkeypatch.setenv("RAG_QDRANT_MODE", "memory")
    monkeypatch.setenv("RAG_TEST_NETWORK", "offline")
    process = multiprocessing.get_context("fork").Process(target=_serve_product)
    process.start()
    base_url = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base_url, timeout=2)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                live = _get_json(client, "/live")
                break
            except httpx.RequestError as error:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "Product Runtime 未在时限内启动。"
                    ) from error
                time.sleep(0.05)
        login = client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": bootstrap_value},
        )
        login.raise_for_status()
        ready = _get_json(client, "/ready")
        components = _get_json(client, "/api/v1/system/components")
        index = client.get("/").text

        assert live == {"status": "live"}
        assert ready["runtime_identity"] == "product-runtime-p10.5"
        assert components
        assert "知识库工作台" in index
    finally:
        client.close()
        process.terminate()
        process.join(timeout=10)


def test_legacy_serve_rejects_product_data_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path / "product-data"))
    with pytest.raises(ValueError, match="禁止复用"):
        main(["legacy-serve"])
