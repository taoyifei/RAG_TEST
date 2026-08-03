from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest

from scripts import verify_model_fleet as fleet
from scripts.verify_model_contracts import (
    DeploymentManifestV2Spec,
    ModelContractOptions,
    build_deployment_manifest_v2,
)

_SOURCE_REVISION = "a" * 40
_ATTEMPT_ID = "b" * 32
_EMBEDDING_TOKENIZER_REVISION = (
    "sha256:def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"
)
_LLM_TOKENIZER_REVISION = (
    "sha256:aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
)
_ENDPOINTS = (
    "http://embedding.internal:8000",
    "http://reranker.internal:8000",
    "http://llm-a.internal:8000",
    "http://llm-a.internal:8001",
    "http://llm-b.internal:8000",
    "http://llm-b.internal:8001",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _write_manifests(directory: Path) -> None:
    directory.mkdir()
    specifications = (
        ("embedding.json", "embedding", _ENDPOINTS[0]),
        ("reranker.json", "reranker", _ENDPOINTS[1]),
        ("llm-1.json", "llm", _ENDPOINTS[2]),
        ("llm-2.json", "llm", _ENDPOINTS[3]),
        ("llm-3.json", "llm", _ENDPOINTS[4]),
        ("llm-4.json", "llm", _ENDPOINTS[5]),
    )
    for index, (name, service, endpoint) in enumerate(
        specifications,
        start=1,
    ):
        model = {
            "embedding": "Qwen3-Embedding-0.6B",
            "reranker": "Qwen3-Reranker-0.6B",
            "llm": "Qwen/Qwen3-8B-AWQ",
        }[service]
        contract: dict[str, object]
        if service == "embedding":
            contract = {"dimension": 1024}
        elif service == "reranker":
            contract = {"score_min": 0.0, "score_max": 1.0}
        else:
            contract = {
                "quantization": "awq",
                "max_context_tokens": 32768,
                "chat_template_sha256": "sha256:" + "c" * 64,
            }
        model_revision = (
            "llm-model-revision"
            if service == "llm"
            else f"model-revision-{index}"
        )
        tokenizer_revision = {
            "embedding": _EMBEDDING_TOKENIZER_REVISION,
            "reranker": "reranker-tokenizer-revision",
            "llm": _LLM_TOKENIZER_REVISION,
        }[service]
        payload = build_deployment_manifest_v2(
            DeploymentManifestV2Spec(
                service=service,  # type: ignore[arg-type]
                endpoint=endpoint,
                model=model,
                model_revision=model_revision,
                tokenizer_revision=tokenizer_revision,
                runtime_name="test-runtime",
                runtime_version="1.2.3",
                runtime_revision="d" * 40,
                service_contract=contract,
            )
        )
        path = directory / name
        path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o444)


def _rewrite_manifest(path: Path, **updates: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    canonical_payload = {
        key: value
        for key, value in payload.items()
        if key != "manifest_sha256"
    }
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload["manifest_sha256"] = (
        "sha256:" + hashlib.sha256(canonical).hexdigest()
    )
    path.chmod(0o600)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    path.chmod(0o444)


def _environment() -> dict[str, str]:
    return {
        "RAG_EMBEDDING_ENDPOINTS": json.dumps([_ENDPOINTS[0]]),
        "RAG_RERANKER_ENDPOINTS": json.dumps([_ENDPOINTS[1]]),
        "RAG_LLM_ENDPOINTS": json.dumps(list(_ENDPOINTS[2:])),
        "RAG_EMBEDDING_MODEL": "Qwen3-Embedding-0.6B",
        "RAG_RERANKER_MODEL": "Qwen3-Reranker-0.6B",
        "RAG_LLM_MODEL": "Qwen/Qwen3-8B-AWQ",
        "RAG_EMBEDDING_API_TOKEN": "embedding-secret",
        "RAG_RERANKER_API_TOKEN": "reranker-secret",
        "RAG_LLM_API_TOKEN": "llm-secret",
    }


def _options(tmp_path: Path, manifests: Path) -> fleet.FleetVerificationOptions:
    root = _project_root()
    return fleet.FleetVerificationOptions(
        pipeline_path=root / "deployment/config/pipeline.json",
        retrieval_path=root / "deployment/config/retrieval.json",
        llm_tokenizer_path=(
            root / "deployment/assets/tokenizers/llm/tokenizer.json"
        ),
        deployment_manifest_directory=manifests,
        output_directory=tmp_path / "attempt",
        source_revision=_SOURCE_REVISION,
        timeout_seconds=10.0,
        attempt_id=_ATTEMPT_ID,
    )


def _passed_report(options: ModelContractOptions) -> dict[str, object]:
    assert options.deployment_manifest is not None
    manifest = json.loads(
        options.deployment_manifest.read_text(encoding="utf-8")
    )
    return {
        "schema_version": "1",
        "status": "passed",
        "service": options.service,
        "endpoint": options.endpoint,
        "model": options.model,
        "endpoint_revision": options.expected_revision,
        "revision_source": "deployment_manifest",
        "health": "passed",
        "model_id": "passed",
        "probe": {},
        "deployment_manifest_sha256": manifest["manifest_sha256"],
    }


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_fleet_verification_publishes_exact_read_only_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    observed = []

    def fake_verify(
        options: ModelContractOptions,
        *,
        client: httpx.Client,
    ) -> dict[str, object]:
        del client
        observed.append(options)
        return _passed_report(options)

    monkeypatch.setattr(fleet, "verify_model_contract", fake_verify)
    options = _options(tmp_path, manifests)
    with httpx.Client() as client:
        summary = fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert len(observed) == 6
    assert [item.service for item in observed] == [
        "embedding",
        "reranker",
        "llm",
        "llm",
        "llm",
        "llm",
    ]
    assert summary["attempt_id"] == _ATTEMPT_ID
    assert summary["source_revision"] == _SOURCE_REVISION
    assert set(summary) == {
        "schema_version",
        "attempt_id",
        "source_revision",
        "status",
        "reports",
    }
    serialized_summary = json.dumps(summary)
    assert all(endpoint not in serialized_summary for endpoint in _ENDPOINTS)
    assert "secret" not in serialized_summary
    output_names = tuple(
        path.name
        for path in sorted(options.output_directory.iterdir())
    )
    assert output_names == (
        "FLEET_REPORT.json",
        "model-contract-embedding.json",
        "model-contract-llm-1.json",
        "model-contract-llm-2.json",
        "model-contract-llm-3.json",
        "model-contract-llm-4.json",
        "model-contract-reranker.json",
    )
    assert stat.S_IMODE(options.output_directory.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o400
        for path in options.output_directory.iterdir()
    )
    summary_path = options.output_directory / "FLEET_REPORT.json"
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    assert summary["reports"] == [
        {
            "name": path.name,
            "service": json.loads(path.read_text(encoding="utf-8"))[
                "service"
            ],
            "sha256": _sha256(path),
        }
        for path in sorted(
            options.output_directory.glob("model-contract-*.json")
        )
    ]


def test_fleet_verification_rejects_tampered_manifest_digest_before_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    manifest_path = manifests / "embedding.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_sha256"] = "sha256:" + "f" * 64
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    manifest_path.chmod(0o444)
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="严格校验",
    ):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert not options.output_directory.exists()


def test_fleet_verification_rejects_report_manifest_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)

    def fake_verify(
        options: ModelContractOptions,
        *,
        client: httpx.Client,
    ) -> dict[str, object]:
        del client
        report = _passed_report(options)
        report["deployment_manifest_sha256"] = "sha256:" + "f" * 64
        return report

    monkeypatch.setattr(fleet, "verify_model_contract", fake_verify)
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="报告身份",
    ):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert not options.output_directory.exists()


@pytest.mark.parametrize(
    ("manifest_name", "tokenizer_revision"),
    (
        ("embedding.json", "sha256:" + "0" * 64),
        ("llm-1.json", "sha256:" + "1" * 64),
    ),
)
def test_fleet_verification_binds_pipeline_tokenizer_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_name: str,
    tokenizer_revision: str,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    _rewrite_manifest(
        manifests / manifest_name,
        tokenizer_revision=tokenizer_revision,
    )
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="tokenizer revision",
    ):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert not options.output_directory.exists()


@pytest.mark.parametrize(
    "drift_field",
    ("model_revision", "runtime", "service_contract"),
)
def test_fleet_verification_rejects_llm_replica_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_field: str,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    manifest_path = manifests / "llm-4.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    drifted: object
    if drift_field == "model_revision":
        drifted = "drifted-llm-model-revision"
    elif drift_field == "runtime":
        drifted = {**payload["runtime"], "version": "9.9.9"}
    else:
        drifted = {**payload["service_contract"], "quantization": "gptq"}
    _rewrite_manifest(manifest_path, **{drift_field: drifted})
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="四个 LLM",
    ):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert not options.output_directory.exists()


@pytest.mark.parametrize(
    ("environment_name", "endpoints", "expected_count"),
    (
        ("RAG_EMBEDDING_ENDPOINTS", (), 1),
        (
            "RAG_RERANKER_ENDPOINTS",
            (_ENDPOINTS[1], "http://reranker-copy.internal:8000"),
            1,
        ),
        ("RAG_LLM_ENDPOINTS", _ENDPOINTS[2:5], 4),
    ),
)
def test_fleet_verification_rejects_endpoint_count_before_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    endpoints: tuple[str, ...],
    expected_count: int,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    environment = _environment()
    environment[environment_name] = json.dumps(endpoints)
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match=f"恰含 {expected_count}",
    ):
        fleet.verify_model_fleet(
            options,
            environment=environment,
            client=client,
        )

    assert not options.output_directory.exists()


def test_fleet_verification_rejects_cross_service_endpoint_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    environment = _environment()
    environment["RAG_RERANKER_ENDPOINTS"] = json.dumps([_ENDPOINTS[0]])
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="六个唯一端点",
    ):
        fleet.verify_model_fleet(
            options,
            environment=environment,
            client=client,
        )

    assert not options.output_directory.exists()


def test_fleet_verification_rejects_non_origin_endpoint_before_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    environment = _environment()
    environment["RAG_EMBEDDING_ENDPOINTS"] = json.dumps(
        [f"{_ENDPOINTS[0]}/v1"]
    )
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="origin 根 URL",
    ):
        fleet.verify_model_fleet(
            options,
            environment=environment,
            client=client,
        )

    assert not options.output_directory.exists()


def test_fleet_verification_converts_contract_failure_and_cleans_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)

    def fail_contract(
        options: ModelContractOptions,
        *,
        client: httpx.Client,
    ) -> dict[str, object]:
        del options, client
        raise fleet.ContractError("PROBE_RESPONSE_INVALID")

    monkeypatch.setattr(fleet, "verify_model_contract", fail_contract)
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="PROBE_RESPONSE_INVALID",
    ):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert not options.output_directory.exists()
    assert not tuple(tmp_path.glob(".attempt.*"))


def test_fleet_verification_cleans_partial_attempt_on_probe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    call_count = 0

    def fail_second(
        options: ModelContractOptions,
        *,
        client: httpx.Client,
    ) -> dict[str, object]:
        nonlocal call_count
        del client
        call_count += 1
        if call_count == 2:
            raise ValueError("simulated failure")
        return _passed_report(options)

    monkeypatch.setattr(fleet, "verify_model_contract", fail_second)
    options = _options(tmp_path, manifests)

    with httpx.Client() as client, pytest.raises(
        ValueError,
        match="simulated failure",
    ):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert not options.output_directory.exists()
    assert not tuple(tmp_path.glob(".attempt.*"))


def test_fleet_verification_never_overwrites_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    options = _options(tmp_path, manifests)
    options.output_directory.mkdir()
    sentinel = options.output_directory / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(
        fleet,
        "verify_model_contract",
        lambda *_args, **_kwargs: pytest.fail("不得请求模型端点"),
    )

    with httpx.Client() as client, pytest.raises(FileExistsError):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_fleet_verification_never_overwrites_concurrent_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    _write_manifests(manifests)
    options = _options(tmp_path, manifests)
    call_count = 0

    def publish_racer(
        contract_options: ModelContractOptions,
        *,
        client: httpx.Client,
    ) -> dict[str, object]:
        nonlocal call_count
        del client
        call_count += 1
        if call_count == 6:
            options.output_directory.mkdir()
            (options.output_directory / "sentinel").write_text(
                "racer\n",
                encoding="utf-8",
            )
        return _passed_report(contract_options)

    monkeypatch.setattr(fleet, "verify_model_contract", publish_racer)

    with httpx.Client() as client, pytest.raises(FileExistsError):
        fleet.verify_model_fleet(
            options,
            environment=_environment(),
            client=client,
        )

    assert (options.output_directory / "sentinel").read_text(
        encoding="utf-8"
    ) == "racer\n"
    assert not tuple(tmp_path.glob(".attempt.*"))
