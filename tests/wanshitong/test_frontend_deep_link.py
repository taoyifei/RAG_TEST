"""验证湾事通管理页深层地址的资源路径。"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_app.api.product import _mount_frontend


@pytest.mark.parametrize("root_path", ["", "/kb"])
def test_admin_history_deep_link_loads_assets(
    tmp_path: Path, root_path: str
) -> None:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("// built", encoding="utf-8")
    (frontend / "index.html").write_text(
        '<script src="./assets/app.js"></script>', encoding="utf-8"
    )
    app = FastAPI(root_path=root_path)
    _mount_frontend(app, frontend)

    with TestClient(app) as client:
        response = client.get(root_path + "/admin/history")
        assert response.status_code == 200
        assert f'src="{root_path}/assets/app.js"' in response.text
        assert response.headers["cache-control"] == "no-store"
        asset = client.get(root_path + "/assets/app.js")
        assert asset.status_code == 200
