from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(root: Path, relative_paths: list[str]) -> str:
    content = "".join(
        f"{_sha256(root / relative_path)}  {relative_path}\n"
        for relative_path in sorted(relative_paths)
    )
    return content


def _write_assets(root: Path) -> None:
    required_files = (
        "models/Qwen3-Embedding-0.6B/config.json",
        "models/Qwen3-Embedding-0.6B/model.safetensors",
        "models/Qwen3-Embedding-0.6B/tokenizer.json",
        "models/Qwen3-Reranker-0.6B/config.json",
        "models/Qwen3-Reranker-0.6B/model.safetensors",
        "models/Qwen3-Reranker-0.6B/tokenizer.json",
        (
            "images/ghcr.m.daocloud.io_huggingface_"
            "text-embeddings-inference_1.9.tar"
        ),
        "images/covlink-rerank-api_server.tar",
    )
    for relative_path in required_files:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative_path, encoding="utf-8")

    embedding_files = [
        relative_path
        for relative_path in required_files
        if relative_path.startswith("models/Qwen3-Embedding")
    ]
    reranker_files = [
        relative_path
        for relative_path in required_files
        if relative_path.startswith("models/Qwen3-Reranker")
    ]
    embedding_manifest = root / "manifests/Qwen3-Embedding-0.6B.sha256"
    reranker_manifest = root / "manifests/Qwen3-Reranker-0.6B.sha256"
    embedding_manifest.parent.mkdir(parents=True)
    embedding_manifest.write_text(
        _write_manifest(root, embedding_files),
        encoding="utf-8",
    )
    reranker_manifest.write_text(
        _write_manifest(root, reranker_files),
        encoding="utf-8",
    )

    image_manifests = {
        (
            "manifests/ghcr.m.daocloud.io_huggingface_"
            "text-embeddings-inference_1.9.tar.sha256"
        ): (
            "images/ghcr.m.daocloud.io_huggingface_"
            "text-embeddings-inference_1.9.tar"
        ),
        "manifests/covlink-rerank-api_server.tar.sha256": (
            "images/covlink-rerank-api_server.tar"
        ),
    }
    for manifest_path, image_path in image_manifests.items():
        (root / manifest_path).write_text(
            _write_manifest(root, [image_path]),
            encoding="utf-8",
        )

    revisions = root / "MODEL_REVISIONS.env"
    revisions.write_text(
        "EMBEDDING_REVISION=sha256:"
        f"{_sha256(embedding_manifest)}\n"
        "RERANKER_REVISION=sha256:"
        f"{_sha256(reranker_manifest)}\n",
        encoding="utf-8",
    )
    master_paths = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    ]
    (root / "MANIFEST.sha256").write_text(
        _write_manifest(root, master_paths),
        encoding="utf-8",
    )


def _write_fake_commands(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/bin/sh
if [ "$1" = "compose" ]; then
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  case "$*" in
    *--format*) printf '%s\\n' 'linux/amd64' ;;
  esac
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    nvidia_smi = bin_dir / "nvidia-smi"
    nvidia_smi.write_text(
        "#!/bin/sh\nprintf '%s\\n' 0 1 2 3\n",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)


def _write_env(path: Path, asset_root: str) -> None:
    path.write_text(
        f"RAG_MODEL_ASSET_ROOT={asset_root}\n"
        "RAG_MODEL_BIND_ADDRESS=0.0.0.0\n"
        "RAG_EMBEDDING_PORT=8091\n"
        "RAG_RERANKER_PORT=8092\n"
        "RAG_EMBEDDING_GPU_DEVICE_ID=1\n"
        "RAG_RERANKER_GPU_DEVICE_ID=2\n",
        encoding="utf-8",
    )


def _run_preflight(
    tmp_path: Path,
    *,
    mutate_env: str | None = None,
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).parents[1]
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_assets(assets)
    env_file = tmp_path / "model-services.env"
    _write_env(env_file, str(assets))
    if mutate_env is not None:
        env_file.write_text(
            mutate_env.replace("__ASSET_ROOT__", str(assets)),
            encoding="utf-8",
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_commands(bin_dir)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    return subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(root / "deployment/model-services/preflight.sh"),
            str(env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_model_services_compose_is_offline_and_gpu_pinned() -> None:
    root = Path(__file__).parents[1]
    compose_path = root / "deployment/model-services/compose.yaml"
    compose_text = compose_path.read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    embedding = compose["services"]["rag-embedding"]
    reranker = compose["services"]["rag-reranker"]

    assert embedding["image"] == (
        "ghcr.m.daocloud.io/huggingface/text-embeddings-inference:1.9"
    )
    assert reranker["image"] == "covlink-rerank-api:server"
    assert embedding["pull_policy"] == "never"
    assert reranker["pull_policy"] == "never"
    assert "build:" not in compose_text
    assert "docker pull" not in compose_text
    assert "--served-model-name" in embedding["command"]
    assert "Qwen3-Embedding-0.6B" in embedding["command"]
    assert "last-token" in embedding["command"]
    assert "--auto-truncate" in embedding["command"]
    assert "false" in embedding["command"]
    assert embedding["command"][
        embedding["command"].index("--max-batch-tokens") + 1
    ] == "32768"
    assert reranker["environment"]["RERANK_MODEL_PATH"] == (
        "/models/Qwen3-Reranker-0.6B"
    )
    assert reranker["environment"]["RERANK_DEVICE"] == "cuda:0"
    assert reranker["environment"]["RERANK_PORT"] == "8001"
    assert embedding["volumes"] == [
        "${RAG_MODEL_ASSET_ROOT:?required}/models/"
        "Qwen3-Embedding-0.6B:/models/Qwen3-Embedding-0.6B:ro"
    ]
    assert reranker["volumes"] == [
        "${RAG_MODEL_ASSET_ROOT:?required}/models/"
        "Qwen3-Reranker-0.6B:/models/Qwen3-Reranker-0.6B:ro"
    ]
    assert embedding["read_only"] is True
    assert reranker["read_only"] is True
    assert embedding["cap_drop"] == ["ALL"]
    assert reranker["cap_drop"] == ["ALL"]
    embedding_device = embedding["deploy"]["resources"]["reservations"][
        "devices"
    ][0]
    reranker_device = reranker["deploy"]["resources"]["reservations"][
        "devices"
    ][0]
    assert embedding_device["device_ids"] == [
        "${RAG_EMBEDDING_GPU_DEVICE_ID:?required}"
    ]
    assert reranker_device["device_ids"] == [
        "${RAG_RERANKER_GPU_DEVICE_ID:?required}"
    ]


def test_model_services_preflight_accepts_valid_assets(tmp_path: Path) -> None:
    result = _run_preflight(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith(
        "RAG_MODEL_SERVICES_PREFLIGHT_OK"
    )


@pytest.mark.parametrize(
    ("env_content", "expected_error"),
    [
        (
            """RAG_MODEL_ASSET_ROOT=__ASSET_ROOT__
RAG_MODEL_BIND_ADDRESS=0.0.0.0
RAG_EMBEDDING_PORT=8091
RAG_RERANKER_PORT=8092
RAG_EMBEDDING_GPU_DEVICE_ID=1
RAG_RERANKER_GPU_DEVICE_ID=1
""",
            "must use different host GPUs",
        ),
        (
            """RAG_MODEL_ASSET_ROOT=__ASSET_ROOT__
RAG_MODEL_BIND_ADDRESS=0.0.0.0
RAG_EMBEDDING_PORT=8091
RAG_RERANKER_PORT=8091
RAG_EMBEDDING_GPU_DEVICE_ID=1
RAG_RERANKER_GPU_DEVICE_ID=2
""",
            "host ports must differ",
        ),
        (
            """RAG_MODEL_ASSET_ROOT=relative-assets
RAG_MODEL_BIND_ADDRESS=0.0.0.0
RAG_EMBEDDING_PORT=8091
RAG_RERANKER_PORT=8092
RAG_EMBEDDING_GPU_DEVICE_ID=1
RAG_RERANKER_GPU_DEVICE_ID=2
""",
            "must be an absolute path",
        ),
    ],
)
def test_model_services_preflight_rejects_unsafe_topology(
    tmp_path: Path,
    env_content: str,
    expected_error: str,
) -> None:
    result = _run_preflight(tmp_path, mutate_env=env_content)

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_model_services_files_contain_no_network_or_secret_action() -> None:
    root = Path(__file__).parents[1]
    directory = root / "deployment/model-services"
    preflight = (directory / "preflight.sh").read_text(encoding="utf-8")
    compose = (directory / "compose.yaml").read_text(encoding="utf-8")
    env_example = (directory / ".env.example").read_text(encoding="utf-8")

    for forbidden in (
        "docker build",
        "docker pull",
        "curl http",
        "curl https",
        "wget ",
        "pip install",
        "apt-get",
        "PASSWORD=",
        "TOKEN=",
    ):
        assert forbidden not in "\n".join(
            (preflight, compose, env_example)
        )


def test_model_services_template_is_packaged_and_verified() -> None:
    root = Path(__file__).parents[1]
    package = (root / "deployment/package.sh").read_text(encoding="utf-8")
    verifier = (root / "deployment/verify-offline.sh").read_text(
        encoding="utf-8"
    )

    for relative_path in (
        "model-services/compose.yaml",
        "model-services/.env.example",
        "model-services/preflight.sh",
        "model-services/README.md",
    ):
        assert relative_path in package
        assert f'"{relative_path}"' in verifier
    assert 'chmod 0700 "${runtime_root}/model-services/preflight.sh"' in (
        package
    )
