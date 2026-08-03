"""最终部署事务的静态安全边界测试。"""

import re
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_DEPLOYMENT_DOCUMENTS = (
    Path("deployment/README.md"),
    Path("design/public/offline-build-and-server-deployment.md"),
)
_LONG_DEPLOYMENT_DOCUMENT = Path(
    "design/public/offline-build-and-server-deployment.md"
)
_ACTIVE_ENV = "/data/tyf/RAG/shared/env/rag.env"
_CANDIDATE_DIR = "/data/tyf/RAG/shared/env/candidates"
_CANDIDATE_ENV = f"{_CANDIDATE_DIR}/${{release_id}}.env"
_REVISION_COMMAND = 'revision="$(git rev-parse HEAD)"'
_DEFAULT_RELEASE_ID_COMMAND = 'release_id="${revision:0:12}"'
_CANONICAL_REPO_DIGESTS = (
    "python@sha256:"
    "86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1",
    "paddlepaddle/paddle@sha256:"
    "bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776",
    "qdrant/qdrant@sha256:"
    "0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286",
)


def _script(name: str) -> str:
    return (_ROOT / "deployment" / name).read_text(encoding="utf-8")


def _document(path: Path) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def _normalize_shell_continuations(text: str) -> str:
    without_continuations = re.sub(r"\\\s*\n\s*", " ", text)
    return " ".join(without_continuations.split())


def test_documents_follow_candidate_and_active_env_contract() -> None:
    deploy = _script("deploy.sh")
    rollback = _script("rollback.sh")
    assert 'candidate_dir="${shared_env_dir}/candidates"' in deploy
    assert 'candidate_env="${1:-}"' in deploy
    assert 'requested_env="${1:-${active_env}}"' in rollback

    detailed_document = _normalize_shell_continuations(
        _document(_LONG_DEPLOYMENT_DOCUMENT)
    )
    assert 'install -d -m 0700 "${candidate_dir}"' in detailed_document
    assert (
        'install -m 0600 "${release_dir}/.env.example" '
        '"${candidate}"'
    ) in detailed_document
    assert (
        'install -m 0600 "${active_env}" "${candidate}"'
        in detailed_document
    )
    assert '"${EDITOR:-vi}" "${candidate}"' in detailed_document

    for path in _DEPLOYMENT_DOCUMENTS:
        document = _normalize_shell_continuations(_document(path))
        assert _CANDIDATE_DIR in document
        assert "test ! -e" in document
        assert re.search(
            r'deploy\.sh"?\s+"?\$\{candidate\}"?',
            document,
        )
        assert not re.search(
            rf'deploy\.sh"?\s+{re.escape(_ACTIVE_ENV)}',
            document,
        )
        assert re.search(
            rf'rollback\.sh"?\s+{re.escape(_ACTIVE_ENV)}',
            document,
        )
        assert (
            "active rag.env 只能由 deploy.sh 成功后发布"
            in document.replace("`", "")
        )


def test_documents_distinguish_release_id_from_source_revision() -> None:
    package = _script("package.sh")
    deploy = _script("deploy.sh")
    assert 'release_id="${RELEASE_ID:-${git_revision:0:12}}"' in package
    assert (
        'printf \'%s\\n\' "${release_id}" > "${runtime_root}/RELEASE_ID"'
        in package
    )
    assert (
        'printf \'%s\\n\' "${git_revision}" '
        '> "${runtime_root}/SOURCE_REVISION"'
        in package
    )
    assert 'release_id="$(cat "${release_dir}/RELEASE_ID")"' in deploy
    assert (
        'source_revision="$(cat "${release_dir}/SOURCE_REVISION")"'
        in deploy
    )
    assert (
        'candidate_env}" != "${candidate_dir}/${release_id}.env"'
        in deploy
    )

    forbidden_assignment = re.compile(
        r"release_id\s*=\s*['\"](?:<|&lt;)[^'\"]*"
        r"40\s*位小写\s*Git\s*SHA",
        re.IGNORECASE,
    )
    detailed_document = _document(_LONG_DEPLOYMENT_DOCUMENT)
    assert 'cat "${runtime_stage}/RELEASE_ID"' in detailed_document
    assert 'cat "${runtime_stage}/SOURCE_REVISION"' in detailed_document
    for path in _DEPLOYMENT_DOCUMENTS:
        document = _document(path)
        normalized = _normalize_shell_continuations(document)
        assert _REVISION_COMMAND in document
        assert _DEFAULT_RELEASE_ID_COMMAND in document
        assert "RELEASE_ID" in document
        assert "SOURCE_REVISION" in document
        assert not forbidden_assignment.search(document)
        assert "release 目录" in document
        assert "镜像 tag" in document
        assert "归档名" in document
        assert "candidate env" in document
        assert re.search(
            r"`?RAG_RELEASE_REVISION`?\s*使用\s*完整\s*`?revision`?",
            document,
        )
        assert "releases/${release_id}" in normalized
        assert _CANDIDATE_ENV in normalized


def test_model_contract_verifier_is_runtime_required_file() -> None:
    package = _script("package.sh")
    verifier = _script("verify-offline.sh")
    runtime_path = (
        "evaluation/runtime/scripts/verify_model_contracts.py"
    )

    assert "scripts/verify_model_contracts.py" in package
    assert (
        '"${runtime_root}/evaluation/runtime/scripts/"'
        in package
    )
    assert f'"{runtime_path}"' in verifier


def test_installed_runtime_documents_model_contract_commands() -> None:
    document = _document(_LONG_DEPLOYMENT_DOCUMENT)

    assert (
        "/data/tyf/RAG/current/evaluation/runtime:"
        "/contract-runtime:ro"
        in document
    )
    assert "--network rag-egress" in document
    assert (
        "--env-file /data/tyf/RAG/shared/env/rag.env"
        in document
    )
    assert (
        "/contract-runtime/scripts/verify_model_contracts.py"
        in document
    )
    assert "/app/deployment/config/retrieval.json" in document
    assert (
        "/app/deployment/assets/tokenizers/llm/tokenizer.json"
        in document
    )
    assert "--token-env RAG_EMBEDDING_API_TOKEN" in document
    assert "--token-env RAG_RERANKER_API_TOKEN" in document
    assert document.count("--token-env RAG_LLM_API_TOKEN") == 4
    assert "run_model_contract model-contract-embedding" in document
    assert "run_model_contract model-contract-reranker" in document
    assert document.count("run_model_contract model-contract-llm-") == 4
    assert 'report_dir="$(mktemp -d \\' in document
    assert '"${logs_root}/model-contract-${release_id}.XXXXXXXX")"' in document
    assert 'python3 - "${report_dir}"' in document
    assert 'paths = sorted(report_dir.glob("model-contract-*.json"))' in document
    assert 'chown -R root:root "${report_dir}"' in document
    assert "报告不含令牌、问题或完整响应" in document


def test_documents_define_provisional_smoke_success() -> None:
    required_statements = (
        "冒烟成功标准",
        "只启动 `rag-app`、`rag-ocr` 和 `rag-qdrant`",
        "`rag-worker` 必须不存在或保持停止",
        "`/live` 必须返回 HTTP 200",
        "Qdrant `/readyz` 必须返回 HTTP 200",
        "OCR `/ready` 必须返回 HTTP 200",
        "CUDA device count 必须大于 0",
        "provisional 阶段 `/ready` 返回 HTTP 503 才是成功",
        "六份模型契约报告全部 `status=passed` 前",
        "不得冻结检索参数或启动 `rag-worker`",
    )

    for path in _DEPLOYMENT_DOCUMENTS:
        document = _document(path)
        for statement in required_statements:
            assert statement in document


def test_document_distinguishes_image_id_from_repo_digests() -> None:
    document = _document(_LONG_DEPLOYMENT_DOCUMENT)
    normalized = _normalize_shell_continuations(document)
    inspect_command = (
        "docker image inspect --format "
        "'{{.Os}}/{{.Architecture}} {{.Id}} "
        "{{range .RepoDigests}}{{println .}}{{end}}'"
    )

    assert inspect_command in normalized
    assert "`.Id` 是本地 image ID" in document
    assert "`.RepoDigests` 用于核验 registry 来源" in document
    assert "不得比较 `.Id == RepoDigest`" in document
    assert "linux/amd64" in document
    for digest in _CANONICAL_REPO_DIGESTS:
        assert digest in document

    forbidden_patterns = (
        r"镜像\s*ID[^。\n]*必须[^。\n]*等于[^。\n]*digest",
        r"\.Id\s*(?:==|=)\s*RepoDigest",
        r"\.Id[^。\n]{0,40}(?:等于|相等)[^。\n]{0,40}RepoDigest",
        r"image\s+ID\s+must\s+equal[^.\n]*digest",
    )
    for path in _DEPLOYMENT_DOCUMENTS:
        candidate = _document(path).replace(
            "不得比较 `.Id == RepoDigest`",
            "",
        )
        for pattern in forbidden_patterns:
            assert not re.search(pattern, candidate, re.IGNORECASE)


def test_deploy_separates_candidate_active_and_late_rollback_state() -> None:
    deploy = _script("deploy.sh")

    assert 'active_env="${shared_env_dir}/rag.env"' in deploy
    assert 'candidate_dir="${shared_env_dir}/candidates"' in deploy
    assert "publish_rollback_state" in deploy
    assert deploy.index("wait_for_runtime_health") < deploy.index(
        "publish_rollback_state"
    )
    assert deploy.index("commit_candidate_env") < deploy.index(
        "publish_rollback_state"
    )


def test_deploy_and_rollback_share_complete_health_gate() -> None:
    for name in ("deploy.sh", "rollback.sh"):
        script = _script(name)
        assert "wait_for_runtime_health" in script
        expected_waits = {
            "rag-qdrant": "QDRANT_HEALTH_TIMEOUT_SECONDS",
            "rag-ocr": "OCR_HEALTH_TIMEOUT_SECONDS",
            "rag-app": "APP_HEALTH_TIMEOUT_SECONDS",
        }
        for container, timeout in expected_waits.items():
            assert (
                f'"{container}" "${{{timeout}}}"'
                in script
            )
        assert "QDRANT_HEALTH_TIMEOUT_SECONDS=60" in script
        assert "QDRANT_READY_TIMEOUT_SECONDS=60" in script
        assert "APP_HEALTH_TIMEOUT_SECONDS=60" in script
        assert "APP_LIVE_TIMEOUT_SECONDS=60" in script
        assert "OCR_HEALTH_TIMEOUT_SECONDS=240" in script
        assert "deadline" in script
        assert "max_attempts=30" not in script
        assert "/live" in script
        assert "wait_for_qdrant_ready" in script
        assert "docker exec rag-app python -c" in script
        assert 'os.environ["RAG_QDRANT_URL"]' in script
        assert 'os.environ["RAG_QDRANT_API_KEY"]' in script
        assert "response.status" in script
        assert "/readyz" in script
        assert 'http://127.0.0.1:${port}/ready' not in script


def test_rollback_records_and_restores_degraded_runtime() -> None:
    rollback = _script("rollback.sh")

    assert "capture_container_state" in rollback
    assert "restore_original_runtime" in rollback
    assert "回滚调用前的核心容器必须全部运行" not in rollback
    assert "ORIGINAL_APP_EXISTS" in rollback
    assert "ORIGINAL_OCR_EXISTS" in rollback
    assert "ORIGINAL_QDRANT_EXISTS" in rollback
