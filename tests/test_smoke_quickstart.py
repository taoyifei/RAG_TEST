"""验证唯一 smoke 操作文档的最短主路径。"""

from pathlib import Path


def test_smoke_quickstart_is_short_and_orders_single_primary_path() -> None:
    """锁定 200 行内的七文件 smoke 发布顺序。"""
    root = Path(__file__).parents[1]
    quickstart = root / "deployment/README.md"
    content = quickstart.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert len(lines) <= 200
    ordered_markers = (
        "## 1. 本地 preflight",
        "scripts/release_smoke.py",
        "## 2. 上传恰好七个文件",
        "## 3. 服务器校验、解包与 server-preflight.sh",
        "## 4. install 与 deploy",
        'bash "${release_dir}/deploy.sh"',
        "## 5. 冒烟验证",
    )
    positions = [content.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert "Docker Engine 29" not in content
    assert "containerd image store" not in content
    assert "完整参考" in content
    assert "design/public/offline-build-and-server-deployment.md" in content
