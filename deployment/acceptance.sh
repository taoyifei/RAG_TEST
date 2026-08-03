#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
releases_dir="${project_root}/releases"
active_env="${project_root}/shared/env/rag.env"
current_link="${project_root}/current"
logs_dir="${project_root}/logs"
temporary_dir=""

fail() {
  echo "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n "${temporary_dir}" \
    && -d "${temporary_dir}" \
    && "${temporary_dir}" == "${logs_dir}"/.acceptance.* ]]; then
    find -P "${temporary_dir}" -depth -delete
  fi
}
trap cleanup EXIT

require_regular_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    fail "${label} 必须是非符号链接的普通文件。"
  fi
}

assert_no_symlink_ancestors() {
  local path="$1"
  local anchor="$2"
  local current
  local relative

  case "${path}" in
    "${anchor}" | "${anchor}"/*) ;;
    *) fail "路径越出固定部署根目录。" ;;
  esac
  current="${anchor}"
  relative="${path#"${anchor}"}"
  relative="${relative#/}"
  while [[ -n "${relative}" ]]; do
    current="${current}/${relative%%/*}"
    if [[ -L "${current}" ]]; then
      fail "受信路径不能包含符号链接。"
    fi
    if [[ "${relative}" == */* ]]; then
      relative="${relative#*/}"
    else
      relative=""
    fi
  done
}

exact_env_value() {
  local file="$1"
  local key="$2"
  local count
  local line

  count="$(grep -c -E "^${key}=" "${file}" || true)"
  if [[ "${count}" -ne 1 ]]; then
    fail "活动环境文件中的 ${key} 必须恰好出现一次。"
  fi
  line="$(grep -E "^${key}=" "${file}")"
  printf '%s' "${line#*=}"
}

container_state() {
  local container="$1"
  docker container inspect \
    --format '{{.State.Running}} {{.State.OOMKilled}}' \
    "${container}"
}

require_container() {
  local container="$1"
  local expected_image_id="$2"
  local actual_image_id
  local oom_killed
  local running

  if ! read -r running oom_killed < <(container_state "${container}"); then
    fail "容器状态读取失败：${container}。"
  fi
  if [[ "${running}" != "true" || "${oom_killed}" != "false" ]]; then
    fail "容器未运行或发生 OOM：${container}。"
  fi
  actual_image_id="$(docker container inspect \
    --format '{{.Image}}' "${container}")"
  if [[ "${actual_image_id}" != "${expected_image_id}" ]]; then
    fail "容器镜像与活动环境不一致：${container}。"
  fi
}

if [[ "$#" -ne 1 || -z "$1" || "$1" != /* ]]; then
  fail "用法：acceptance.sh /data/tyf/RAG/.../模型契约报告目录"
fi
if [[ "$(id -u)" -ne 0 ]]; then
  fail "验收必须由 root 执行，才能生成 root:root 0400 报告。"
fi
if [[ ! -d "${project_root}" || -L "${project_root}" ]]; then
  fail "固定部署根目录不存在或是符号链接。"
fi
if [[ "$(realpath -e "${project_root}")" != "${project_root}" ]]; then
  fail "固定部署根目录不是规范绝对路径。"
fi
assert_no_symlink_ancestors "${active_env}" "${project_root}"
assert_no_symlink_ancestors "${logs_dir}" "${project_root}"
require_regular_file "${active_env}" "活动环境文件"
if [[ "$(stat -c '%a' "${active_env}")" != "600" ]]; then
  fail "活动环境文件权限必须严格为 0600。"
fi
if [[ ! -d "${logs_dir}" || -L "${logs_dir}" ]]; then
  fail "验收日志目录不存在或是符号链接。"
fi
if [[ ! -L "${current_link}" ]]; then
  fail "current 必须是指向活动 release 的符号链接。"
fi

release_dir="$(realpath -e "${current_link}")"
releases_real="$(realpath -e "${releases_dir}")"
if [[ "$(dirname "${release_dir}")" != "${releases_real}" ]]; then
  fail "current 必须直接指向 releases 下的单个 release。"
fi
assert_no_symlink_ancestors "${release_dir}" "${project_root}"
require_regular_file "${release_dir}/RELEASE_ID" "release ID 文件"
require_regular_file "${release_dir}/SOURCE_REVISION" "源码 revision 文件"
require_regular_file "${release_dir}/compose.yaml" "Compose 文件"
require_regular_file "${release_dir}/verify-offline.sh" "离线校验脚本"

release_id="$(cat "${release_dir}/RELEASE_ID")"
source_revision="$(cat "${release_dir}/SOURCE_REVISION")"
if [[ ! "${release_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ \
  || "$(basename "${release_dir}")" != "${release_id}" ]]; then
  fail "活动 release ID 无效或与目录名不一致。"
fi
if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  fail "活动源码 revision 不是 40 位小写 Git SHA。"
fi
if [[ "$(exact_env_value "${active_env}" RAG_RELEASE_REVISION)" \
  != "${source_revision}" ]]; then
  fail "活动环境 revision 与 current release 不一致。"
fi

model_report_input="$1"
assert_no_symlink_ancestors "${model_report_input}" "${project_root}"
if [[ ! -d "${model_report_input}" || -L "${model_report_input}" ]]; then
  fail "模型契约报告目录不存在或是符号链接。"
fi
model_report_dir="$(realpath -e "${model_report_input}")"
if [[ "${model_report_dir}" != "${model_report_input}" ]]; then
  fail "模型契约报告目录必须使用规范绝对路径。"
fi

acceptance_report="${logs_dir}/acceptance-${release_id}.json"
if [[ -e "${acceptance_report}" || -L "${acceptance_report}" ]]; then
  fail "验收报告已存在，拒绝覆盖。"
fi
temporary_dir="$(mktemp -d \
  "${logs_dir}/.acceptance.${release_id}.XXXXXXXX")"
chmod 0700 "${temporary_dir}"
offline_log="${temporary_dir}/verify-offline.log"
frozen_contract_file="${temporary_dir}/frozen-contract.json"
model_summary_file="${temporary_dir}/model-summary.json"
ocr_file="${temporary_dir}/ocr.json"
job_file="${temporary_dir}/job.json"
ready_file="${temporary_dir}/ready.json"
compose_log="${temporary_dir}/compose.log"
report_stage="${temporary_dir}/acceptance.json"

if ! bash "${release_dir}/verify-offline.sh" \
  >"${offline_log}" 2>&1; then
  fail "活动 runtime 离线完整性校验失败。"
fi

app_image="$(exact_env_value "${active_env}" RAG_APP_IMAGE)"
ocr_image="$(exact_env_value "${active_env}" RAG_OCR_IMAGE)"
qdrant_image="$(exact_env_value "${active_env}" RAG_QDRANT_IMAGE)"
port="$(exact_env_value "${active_env}" RAG_PORT)"
for image in "${app_image}" "${ocr_image}" "${qdrant_image}"; do
  if [[ -z "${image}" || "${image}" =~ [[:space:]] \
    || "${image}" == *REPLACE* ]]; then
    fail "活动环境包含无效镜像引用。"
  fi
done
if [[ ! "${port}" =~ ^[0-9]+$ \
  || "${port}" -lt 1 || "${port}" -gt 65535 ]]; then
  fail "活动环境 RAG_PORT 无效。"
fi

app_image_id="$(docker image inspect --format '{{.Id}}' "${app_image}")"
ocr_image_id="$(docker image inspect --format '{{.Id}}' "${ocr_image}")"
qdrant_image_id="$(docker image inspect \
  --format '{{.Id}}' "${qdrant_image}")"
for image_id in "${app_image_id}" "${ocr_image_id}" \
  "${qdrant_image_id}"; do
  if [[ ! "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "活动镜像 ID 无效。"
  fi
done

require_container rag-app "${app_image_id}"
require_container rag-ocr "${ocr_image_id}"
require_container rag-qdrant "${qdrant_image_id}"

if ! docker exec rag-app python -c '
import json
import os
from pathlib import Path

from rag_app.freeze_evidence import FreezeDecision
from rag_app.runtime import load_pipeline
from rag_app.settings import RetrievalSettings
from rag_app.worker_runtime import require_indexable_configuration

pipeline = load_pipeline(Path(os.environ["RAG_PIPELINE_PATH"]))
retrieval = RetrievalSettings.load(Path(os.environ["RAG_RETRIEVAL_PATH"]))
decision = FreezeDecision.load(
    Path("/app/deployment/config/FREEZE_DECISION.json")
)
require_indexable_configuration(pipeline, retrieval, decision)
if decision.sha256() != retrieval.freeze_decision_sha256:
    raise ValueError("freeze decision 摘要不一致")
if decision.index_fingerprint != pipeline.index_fingerprint():
    raise ValueError("索引指纹不一致")
serving_fingerprint = retrieval.serving_fingerprint(pipeline)
if decision.serving_fingerprint != serving_fingerprint:
    raise ValueError("服务指纹不一致")
print(json.dumps({
    "schema_version": "1",
    "embedding_model": pipeline.embedding_model,
    "embedding_revision": pipeline.embedding_revision,
    "embedding_dimension": pipeline.embedding_dimension,
    "reranker_model": pipeline.reranker_model,
    "reranker_revision": pipeline.reranker_revision,
    "llm_model": pipeline.llm_model,
    "llm_revisions": pipeline.llm_revisions,
    "calibration_source_revision": (
        decision.model_revisions.calibration_source_revision
    ),
    "index_fingerprint": pipeline.index_fingerprint(),
    "serving_fingerprint": serving_fingerprint,
    "freeze_decision_sha256": decision.sha256(),
}, separators=(",", ":"), sort_keys=True))
' >"${frozen_contract_file}" 2>"${compose_log}"; then
  fail "活动 app 镜像未通过冻结配置身份校验。"
fi
if [[ ! -s "${frozen_contract_file}" \
  || "$(stat -c '%s' "${frozen_contract_file}")" -gt 32768 ]]; then
  fail "冻结配置身份输出缺失或异常。"
fi

if ! python3 - "${model_report_dir}" "${active_env}" \
  "${frozen_contract_file}" \
  >"${model_summary_file}" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPORT_NAMES = (
    "model-contract-embedding.json",
    "model-contract-reranker.json",
    "model-contract-llm-1.json",
    "model-contract-llm-2.json",
    "model-contract-llm-3.json",
    "model-contract-llm-4.json",
)
REQUIRED_REPORT_FIELDS = {
    "schema_version",
    "status",
    "service",
    "endpoint",
    "model",
    "endpoint_revision",
    "revision_source",
    "health",
    "model_id",
    "probe",
}
OPTIONAL_REPORT_FIELDS = {"deployment_manifest_sha256"}


def fail(message: str) -> None:
    raise ValueError(message)


def load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 不是有效 UTF-8 JSON。") from error
    if type(value) is not dict:
        fail(f"{label} 必须是 JSON object。")
    return value


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            fail("活动环境文件包含重复键。")
        values[key] = value
    return values


def required_env(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or not value or "REPLACE" in value:
        fail("活动环境缺少已定稿的模型变量。")
    return value


def endpoint_array(values: dict[str, str], key: str, count: int) -> list[str]:
    try:
        endpoints = json.loads(required_env(values, key))
    except json.JSONDecodeError as error:
        raise ValueError("模型 endpoint 数组不是有效 JSON。") from error
    if (
        type(endpoints) is not list
        or len(endpoints) != count
        or any(type(endpoint) is not str for endpoint in endpoints)
        or len(set(endpoints)) != count
    ):
        fail("模型 endpoint 数量、类型或唯一性无效。")
    return [normalize_endpoint(endpoint) for endpoint in endpoints]


def normalize_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname.endswith(".invalid")
    ):
        fail("模型 endpoint 不是安全的内网 HTTP URL。")
    return endpoint.strip().rstrip("/")


def require_sha256(value: object, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        fail(f"{label} 不是 SHA256 摘要。")
    return value


def validate_regular_readonly(path: Path) -> None:
    if path.is_symlink():
        fail("模型报告不能是符号链接。")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        fail("模型报告必须是非空普通文件。")
    if metadata.st_size > 1024 * 1024 or metadata.st_mode & 0o222:
        fail("模型报告过大或仍可写。")


def validate_frozen_contract(path: Path, values: dict[str, str]) -> dict[str, object]:
    frozen = load_object(path, "冻结配置身份")
    expected_fields = {
        "schema_version",
        "embedding_model",
        "embedding_revision",
        "embedding_dimension",
        "reranker_model",
        "reranker_revision",
        "llm_model",
        "llm_revisions",
        "calibration_source_revision",
        "index_fingerprint",
        "serving_fingerprint",
        "freeze_decision_sha256",
    }
    if set(frozen) != expected_fields or frozen["schema_version"] != "1":
        fail("冻结配置身份 schema 无效。")
    if frozen["embedding_model"] != required_env(values, "RAG_EMBEDDING_MODEL"):
        fail("Embedding 模型与冻结配置不一致。")
    if frozen["reranker_model"] != required_env(values, "RAG_RERANKER_MODEL"):
        fail("Reranker 模型与冻结配置不一致。")
    if frozen["llm_model"] != required_env(values, "RAG_LLM_MODEL"):
        fail("LLM 模型与冻结配置不一致。")
    if (
        type(frozen["embedding_dimension"]) is not int
        or frozen["embedding_dimension"] <= 0
    ):
        fail("冻结 Embedding 维度无效。")
    for key in (
        "index_fingerprint",
        "serving_fingerprint",
        "freeze_decision_sha256",
    ):
        require_sha256(frozen[key], key)
    if (
        type(frozen["calibration_source_revision"]) is not str
        or re.fullmatch(
            r"[0-9a-f]{40}", frozen["calibration_source_revision"]
        )
        is None
    ):
        fail("冻结 calibration source revision 无效。")
    revisions = frozen["llm_revisions"]
    if (
        type(revisions) is not list
        or len(revisions) != 4
        or any(
            type(item) is not list
            or len(item) != 2
            or item[0] != f"llm-{index}"
            or type(item[1]) is not str
            or not item[1]
            for index, item in enumerate(revisions, start=1)
        )
    ):
        fail("冻结 LLM revision 集合无效。")
    return frozen


def validate_report(
    path: Path,
    *,
    service: str,
    endpoint: str,
    model: str,
    revision: str,
    embedding_dimension: int,
) -> dict[str, object]:
    validate_regular_readonly(path)
    report = load_object(path, "模型契约报告")
    fields = set(report)
    if fields not in (
        REQUIRED_REPORT_FIELDS,
        REQUIRED_REPORT_FIELDS | OPTIONAL_REPORT_FIELDS,
    ):
        fail("模型契约报告字段集合无效。")
    if (
        report["schema_version"] != "1"
        or report["status"] != "passed"
        or report["service"] != service
        or normalize_endpoint(report["endpoint"]) != endpoint
        or report["model"] != model
        or report["endpoint_revision"] != revision
        or report["health"] != "passed"
        or report["model_id"] != "passed"
        or type(report["probe"]) is not dict
    ):
        fail("模型契约报告与活动冻结配置不一致。")
    revision_source = report["revision_source"]
    if revision_source == "endpoint":
        if "deployment_manifest_sha256" in report:
            fail("endpoint revision 报告不应携带部署清单摘要。")
    elif revision_source == "deployment_manifest":
        require_sha256(
            report.get("deployment_manifest_sha256"),
            "模型部署清单",
        )
    else:
        fail("模型 revision 来源无效。")
    probe = report["probe"]
    if service == "embedding":
        if probe.get("dimension") != embedding_dimension:
            fail("Embedding 探测维度与冻结配置不一致。")
    elif service == "reranker" and probe.get("score_range") != [0.0, 1.0]:
        fail("Reranker 分数范围探测无效。")
    return report


def main() -> None:
    report_dir = Path(sys.argv[1])
    env_values = load_env(Path(sys.argv[2]))
    frozen = validate_frozen_contract(Path(sys.argv[3]), env_values)
    expected_entries = set(REPORT_NAMES) | {"FLEET_REPORT.json"}
    actual_entries = {entry.name for entry in report_dir.iterdir()}
    if actual_entries != expected_entries:
        fail("模型报告目录必须恰含六份报告和一份 fleet 汇总。")
    endpoints = (
        endpoint_array(env_values, "RAG_EMBEDDING_ENDPOINTS", 1)
        + endpoint_array(env_values, "RAG_RERANKER_ENDPOINTS", 1)
        + endpoint_array(env_values, "RAG_LLM_ENDPOINTS", 4)
    )
    models = (
        frozen["embedding_model"],
        frozen["reranker_model"],
        *(frozen["llm_model"] for _ in range(4)),
    )
    revisions = (
        frozen["embedding_revision"],
        frozen["reranker_revision"],
        *(item[1] for item in frozen["llm_revisions"]),
    )
    services = ("embedding", "reranker", "llm", "llm", "llm", "llm")
    fleet_entries: list[dict[str, str]] = []
    for name, service, endpoint, model, revision in zip(
        REPORT_NAMES,
        services,
        endpoints,
        models,
        revisions,
        strict=True,
    ):
        report_path = report_dir / name
        validate_report(
            report_path,
            service=service,
            endpoint=endpoint,
            model=model,
            revision=revision,
            embedding_dimension=frozen["embedding_dimension"],
        )
        fleet_entries.append(
            {
                "name": name,
                "service": service,
                "sha256": "sha256:"
                + hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }
        )
    fleet_path = report_dir / "FLEET_REPORT.json"
    validate_regular_readonly(fleet_path)
    fleet = load_object(fleet_path, "fleet 汇总")
    if set(fleet) != {
        "schema_version",
        "attempt_id",
        "source_revision",
        "status",
        "reports",
    }:
        fail("fleet 汇总字段集合无效。")
    if (
        fleet["schema_version"] != "1"
        or type(fleet["attempt_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", fleet["attempt_id"]) is None
        or fleet["source_revision"]
        != frozen["calibration_source_revision"]
        or fleet["status"] != "passed"
        or fleet["reports"]
        != sorted(fleet_entries, key=lambda report: report["name"])
    ):
        fail("fleet 汇总未绑定本次六份报告和 calibration revision。")
    result = {
        "attempt_id": fleet["attempt_id"],
        "model_bundle_sha256": "sha256:"
        + hashlib.sha256(fleet_path.read_bytes()).hexdigest(),
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


try:
    main()
except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
    print(f"模型契约集合校验失败：{error}", file=sys.stderr)
    raise SystemExit(1) from None
PY
then
  fail "模型契约报告集合未通过严格绑定校验。"
fi

if ! docker exec rag-ocr python -c '
import json
import os
import paddle

print(json.dumps({
    "device": os.environ.get("RAG_OCR_DEVICE", ""),
    "cuda_count": int(paddle.device.cuda.device_count()),
}, separators=(",", ":"), sort_keys=True))
' >"${ocr_file}" 2>>"${compose_log}"; then
  fail "OCR 容器 GPU 探测失败。"
fi
if ! python3 - "${ocr_file}" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    valid = (
        type(value) is dict
        and set(value) == {"device", "cuda_count"}
        and type(value["device"]) is str
        and value["device"].startswith("gpu:")
        and type(value["cuda_count"]) is int
        and value["cuda_count"] > 0
    )
except (OSError, UnicodeError, json.JSONDecodeError):
    valid = False
if not valid:
    raise SystemExit(1)
PY
then
  fail "OCR 容器未使用可见 CUDA GPU。"
fi

compose=(
  docker compose --profile index
  --env-file "${active_env}"
  -f "${release_dir}/compose.yaml"
)
if docker container inspect rag-worker >/dev/null 2>&1; then
  if ! read -r worker_running worker_oom < <(container_state rag-worker); then
    fail "后台 worker 状态读取失败。"
  fi
  if [[ "${worker_oom}" != "false" ]]; then
    fail "后台 worker 已发生 OOM。"
  fi
  if [[ "${worker_running}" == "true" ]]; then
    if ! "${compose[@]}" stop rag-worker \
      >>"${compose_log}" 2>&1; then
      fail "停止后台 worker 失败。"
    fi
  fi
  if ! read -r worker_running worker_oom < <(container_state rag-worker) \
    || [[ "${worker_running}" != "false" \
      || "${worker_oom}" != "false" ]]; then
    fail "后台 worker 未可靠停止。"
  fi
fi

set +e
"${compose[@]}" run --rm --no-deps rag-worker index full \
  --idempotency-key "initial-${release_id}" \
  >"${job_file}" 2>>"${compose_log}"
job_exit="$?"
set -e
if [[ "${job_exit}" -ne 0 ]]; then
  fail "一次性全量索引命令失败。"
fi
if ! python3 - "${job_file}" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    value, offset = decoder.raw_decode(raw)
    valid = (
        not raw[offset:].strip()
        and type(value) is dict
        and set(value) == {"job_id", "state", "error_code"}
        and type(value["job_id"]) is str
        and re.fullmatch(r"job_[0-9a-f]{32}", value["job_id"]) is not None
        and value["state"] == "succeeded"
        and value["error_code"] is None
    )
except (OSError, UnicodeError, json.JSONDecodeError):
    valid = False
if not valid:
    raise SystemExit(1)
PY
then
  fail "索引任务没有返回严格 succeeded/error null 终态。"
fi

if ! "${compose[@]}" up -d --no-deps --no-build --pull never rag-worker \
  >>"${compose_log}" 2>&1; then
  fail "启动持久 worker 失败。"
fi
require_container rag-worker "${app_image_id}"
require_container rag-app "${app_image_id}"
require_container rag-ocr "${ocr_image_id}"
require_container rag-qdrant "${qdrant_image_id}"

if ! curl -sS --connect-timeout 2 --max-time 10 \
  --write-out $'\n%{http_code}' \
  "http://127.0.0.1:${port}/ready" >"${ready_file}"; then
  fail "readiness 请求失败。"
fi
if ! python3 - "${ready_file}" <<'PY'
import json
import sys
from pathlib import Path

try:
    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    body, status = raw.rsplit("\n", 1)
    value = json.loads(body)
    valid = (
        status == "200"
        and type(value) is dict
        and value.get("ready") is True
    )
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    valid = False
if not valid:
    raise SystemExit(1)
PY
then
  fail "readiness 必须同时满足 HTTP 200 和 ready=true。"
fi

if ! python3 - "${source_revision}" "${release_id}" \
  "${job_file}" "${model_summary_file}" "${ocr_file}" \
  "${report_stage}" <<'PY'
import json
import re
import sys
from pathlib import Path

source_revision, release_id = sys.argv[1:3]
job = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
model = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
ocr = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
output = Path(sys.argv[6])
if (
    re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", release_id) is None
    or set(model) != {"attempt_id", "model_bundle_sha256"}
):
    raise SystemExit(1)
report = {
    "schema_version": "1",
    "status": "passed",
    "release_id": release_id,
    "source_revision": source_revision,
    "model_attempt_id": model["attempt_id"],
    "model_bundle_sha256": model["model_bundle_sha256"],
    "job": job,
    "ready": True,
    "ocr_gpu": ocr,
}
output.write_text(
    json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
then
  fail "生成脱敏验收报告失败。"
fi
chmod 0400 "${report_stage}"
chown 0:0 "${report_stage}"
if ! mv -Tn "${report_stage}" "${acceptance_report}" \
  || [[ -e "${report_stage}" ]]; then
  fail "验收报告原子发布失败或目标已存在。"
fi
require_regular_file "${acceptance_report}" "验收报告"
if [[ "$(stat -c '%a' "${acceptance_report}")" != "400" ]]; then
  fail "验收报告权限不是 0400。"
fi

echo "ACCEPTANCE_OK release=${release_id} report=${acceptance_report}"
