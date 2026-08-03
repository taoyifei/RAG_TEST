# DOCX RAG 离线构建与服务器部署手册

本文给出由操作人员执行的完整流程：在 WSL 联网准备资产和构建
`linux/amd64` 镜像，生成 runtime/语料双包，分别校验后上传，在
`${RAG_SERVER}` 的 `/data/tyf/RAG` 下安装并完成启动、冒烟和回滚。
服务器不执行 build、pull、pip/apt/npm 安装，也不下载模型或字体。

V1 只处理 DOCX。图片 OCR 使用独立的单 GPU PaddleOCR 容器；EMF 不做
隐式转换，继续记录 `EMF_RASTERIZER_UNAVAILABLE`。当前检索参数仍是候选值，
冻结集验收完成前 `/ready` 必须保持严格，不能为了冒烟改成宽松成功。

## 发布身份约定

`revision` 始终表示完整 40 位小写 Git SHA。`release_id` 是 runtime
`RELEASE_ID` 的值；未显式设置打包变量 `RELEASE_ID` 时，默认值为
`revision` 前 12 位。构建端推荐命令如下：

```bash
revision="$(git rev-parse HEAD)"
release_id="${revision:0:12}"
```

runtime 将两者分别保存为 `SOURCE_REVISION` 和 `RELEASE_ID`，服务器解包后
必须从这两个文件重新读取。`release_id` 统一用于 release 目录、镜像 tag、
归档名和 candidate env 文件名；候选文件中的 `RAG_RELEASE_REVISION` 使用
完整 `revision`，不得使用 `release_id`。

## 1. 交付物和目录

一次发布只原子产生一个 release 输出目录；两个归档各有独立外层摘要，
目录本身另有逐文件摘要：

```text
artifacts/releases/<release-id>-<corpus-id>/
├── RELEASE_MANIFEST.sha256
├── offline_bundle.py
├── offline_bundle.py.sha256
├── rag-runtime-<release-id>.tar.gz
├── rag-runtime-<release-id>.tar.gz.sha256
├── rag-corpus-<corpus-id>.tar.gz
└── rag-corpus-<corpus-id>.tar.gz.sha256
```

服务器目录固定如下。Docker 自身镜像层仍由服务器现有 Docker data-root
管理；项目文件、配置、语料、SQLite 和 Qdrant 数据全部位于本目录。

```text
/data/tyf/RAG/
├── incoming/                 # 上传和临时解包
├── releases/<release-id>/    # 只读发布内容
├── current -> releases/...   # 当前 release
├── shared/
│   ├── corpora/<corpus-id>/  # DOCX 和冻结评测集
│   ├── env/                  # rag.env 和回滚记录
│   └── model-services/       # 独立交付并跨 release 复用的模型服务资产
├── data/
│   ├── state/                # SQLite、任务和索引状态
│   └── qdrant/               # Qdrant 持久化数据
├── backups/                  # SQLite/Qdrant 备份登记
└── logs/                     # 人工验收的脱敏输出
```

其中语料版本目录的规范路径是
`shared/corpora/<corpus-id>/`，不得直接覆盖已经存在的 corpus ID。
持久化相对路径固定为 `data/state/` 与 `data/qdrant/`；备份和脱敏验收输出
分别只写入 `backups/` 与 `logs/`。

不得把 `.env`、回滚记录或持久化数据放进 release 目录。更新 release
不能覆盖语料或状态目录。

## 2. WSL 前置检查

在仓库根目录执行。Docker 与版本检查都必须成功，且当前分支需由操作人员
审核并提交；打包器会拒绝把未提交源码伪装成某个 Git revision。

```bash
export RAG_REPOSITORY='<WSL 中的源码仓库绝对路径>'
cd "${RAG_REPOSITORY}"
docker version
docker compose version
docker buildx version
docker version --format \
  'client={{.Client.Version}} server={{.Server.Version}}'
docker info --format '{{json .DriverStatus}}'
python3.11 --version
git status --short
git rev-parse HEAD
```

构建端与服务器统一使用 Docker Engine 29 和 containerd image store。上述
`DriverStatus` 必须包含
`["driver-type","io.containerd.snapshotter.v1"]`；只有显示
`Storage Driver: overlayfs` 还不够。该约束用于保证执行
`docker save/load --platform linux/amd64` 后可通过 `.Descriptor.digest` 复核单平台 manifest
身份，缺失时停止，不得回退到跳过镜像身份校验。

先运行本地质量门：

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/mypy --no-incremental src evaluation scripts
.venv/bin/python -m pytest -q
bash -n deployment/*.sh
docker compose --env-file deployment/.env.example \
  -f deployment/compose.yaml config -q
git diff --check
```

## 3. 联网准备固定资产

先拉取并检查三张固定 digest 基础镜像；不得换成浮动 tag：

```bash
python_repo_digest='python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1'
ocr_repo_digest='paddlepaddle/paddle@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776'
qdrant_repo_digest='qdrant/qdrant@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286'
python_base="${python_repo_digest}"
ocr_base='paddlepaddle/paddle:3.3.0-gpu-cuda12.6-cudnn9.5@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776'
qdrant_ref='qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286'
docker pull "${python_base}"
docker pull "${ocr_base}"
docker pull "${qdrant_ref}"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}} {{range .RepoDigests}}{{println .}}{{end}}' \
  "${python_base}" "${ocr_base}" "${qdrant_ref}"

verify_base_image() {
  local image_ref="$1"
  local approved_repo_digest="$2"
  test "$(docker image inspect \
    --format '{{.Os}}/{{.Architecture}}' "${image_ref}")" = linux/amd64
  docker image inspect \
    --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image_ref}" \
    | grep -Fx -- "${approved_repo_digest}"
}
verify_base_image "${python_base}" "${python_repo_digest}"
verify_base_image "${ocr_base}" "${ocr_repo_digest}"
verify_base_image "${qdrant_ref}" "${qdrant_repo_digest}"
```

`.Id` 是本地 image ID，只标识当前 Docker daemon 中的镜像对象；
`.RepoDigests` 用于核验 registry 来源。三个固定引用都必须检查为
`linux/amd64`，并确认各自 `.RepoDigests` 精确包含上面批准的 canonical
RepoDigest。不得比较 `.Id == RepoDigest`，两者属于不同身份域。

应用 wheelhouse 必须由 Python 3.11 按 lock 重建。脚本只接受二进制
`linux/amd64` wheels，并检查项目 wheel 同时包含 worker 和 OCR 入口。

```bash
.venv/bin/python scripts/prepare_runtime_wheels.py
(cd deployment/runtime/wheelhouse && \
  sha256sum --check ../WHEELS.sha256)
sha256sum --check deployment/ASSETS.sha256
```

runtime wheel 准备器先在三件套的同一父目录内完整生成并复核新
`wheelhouse`、`WHEELS.sha256` 和 `PROJECT_WHEEL.json`，确认项目 wheel
内嵌 revision 与 clean Git HEAD 一致后才开始替换。替换期间旧三件套先原子
移入事务备份；下载、构建、清单写入或任一移动失败都会恢复旧三者，不会先删除
旧 wheel 或留下新旧混合状态。

仅在 OCR 固定资产尚未装配或批准版本发生变化时执行以下下载。下载脚本按
来源清单、大小和 SHA256 校验，不从服务器下载：

```bash
.venv/bin/python scripts/download_ocr_assets.py
.venv/bin/python scripts/download_ocr_wheels.py
(cd deployment/ocr/assets && sha256sum --check MANIFEST.sha256)
(cd deployment/ocr/assets/wheelhouse && \
  sha256sum --check ../../WHEELS.sha256)
(cd deployment/ocr/assets && sha256sum --check ../MODELS.sha256)
```

任何模型文件变化都必须先更新来源、revision 和固定摘要并复核，不能只改
摘要求通过。

Embedding/Reranker 模型服务是独立的共享基础设施，不进入本次 release 的七个
上传文件。若服务器上的
`/data/tyf/RAG/shared/model-services/qwen3-embedding-reranker-0.6b-v1/`
已经按其独立交付清单完成摘要与 revision 校验，两个服务正在运行，且本手册
第 8 节的 embedding/reranker 模型契约能够通过，则直接复用现有端点：不要重新
上传 8 GB 级模型资产包，不要重复解包或重新加载其镜像。若是 fresh 服务器，
或者目录、摘要、revision、服务健康任一项缺失或不一致，必须在创建 candidate
之前停止，先按独立模型服务交付完成安装和只读验证；本 RAG 双包不会补齐模型
服务。模型资产包不得混入 release delivery，否则会破坏恰好七文件的交付契约。

## 4. 构建和断网自检

将当前提交写入镜像的 OCI revision；两个自建镜像必须使用同一个 40 位
revision。Qdrant 使用已批准的 digest，不使用浮动 tag。

```bash
revision="$(git rev-parse HEAD)"
release_id="${revision:0:12}"
docker buildx build --network none --platform linux/amd64 --load \
  --build-arg "VCS_REF=${revision}" \
  --tag "docx-rag:${release_id}" .
docker buildx build --network none --platform linux/amd64 --load \
  --build-arg "VCS_REF=${revision}" \
  --file deployment/ocr/Dockerfile \
  --tag "docx-rag-ocr:${release_id}" .
```

构建后确认平台、revision，并在完全断网条件下做资源自检。OCR GPU 推理
不在 WSL 执行，留到服务器冒烟阶段。

```bash
docker image inspect \
  --format '{{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "docx-rag:${release_id}" "docx-rag-ocr:${release_id}"
docker run --rm --network none "docx-rag:${release_id}" asset-selfcheck
docker run --rm --network none --entrypoint python \
  "docx-rag-ocr:${release_id}" -c \
  'from rag_app.ocr.main import main; assert callable(main)'
```

预期两行平台均为 `linux/amd64`，revision 均等于 `git rev-parse HEAD`，
两条断网命令退出码均为 0。

应用 wheel 内的 `SOURCE_REVISION` 还会在 `serve`、`worker` 和一次性
`index` 创建 Qdrant、SQLite 或 HTTP 客户端之前，与
`RAG_RELEASE_REVISION` 做精确比较。`development-unset`、缺失、非 40 位
小写 Git SHA 或错配都会使进程立即失败；`build-info` 仍可用于只读诊断。

## 5. 生成和校验双包

先处理经数据负责人明确确认的过期 DOCX。不得根据文件名、日期或当前六个文件
自行猜测。只接受 `docs/` 下的精确相对路径；打包前先移动到 Git 已忽略的
quarantine，以便部署失败时恢复。没有过期文件时保持数组为空并跳过移动：

```bash
(
set -euo pipefail

repo_root="$(pwd -P)"
docs_root="$(realpath -e "${repo_root}/docs")"
quarantine_id="$(date -u +%Y%m%dT%H%M%SZ)"
quarantine_root="${repo_root}/artifacts/docx-quarantine/${quarantine_id}"

# 仅由数据负责人填写精确相对路径；不要在此猜测当前六个 DOCX。
obsolete_doc_paths=(
  # 'RAG资料库/某分类/已确认过期的文档.docx'
)
source_paths=()
target_paths=()

for relative_path in "${obsolete_doc_paths[@]}"; do
  if [[ -z "${relative_path}" || "${relative_path}" == /* \
    || "${relative_path}" == ../* || "${relative_path}" == */../* \
    || "${relative_path}" != *.docx ]]; then
    echo "过期 DOCX 必须是 docs 下的精确相对 .docx 路径。" >&2
    exit 1
  fi
  input_path="${docs_root}/${relative_path}"
  if [[ -L "${input_path}" ]]; then
    echo "过期 DOCX 不能是符号链接：${relative_path}" >&2
    exit 1
  fi
  source_path="$(realpath -e -- "${input_path}")"
  if [[ "${source_path}" != "${docs_root}/"* \
    || ! -f "${source_path}" ]]; then
    echo "过期 DOCX 越界、缺失或不是普通文件：${relative_path}" >&2
    exit 1
  fi
  target_path="${quarantine_root}/${relative_path}"
  test ! -e "${target_path}"
  test ! -L "${target_path}"
  source_paths+=("${source_path}")
  target_paths+=("${target_path}")
done

for index in "${!source_paths[@]}"; do
  install -d -m 0700 "$(dirname "${target_paths[index]}")"
  mv -- "${source_paths[index]}" "${target_paths[index]}"
done

if ((${#obsolete_doc_paths[@]} > 0)); then
  printf 'DOCX_QUARANTINE=%s\n' "${quarantine_root}"
  echo '文档集合已变化：必须使用新的 corpus_id，禁止复用旧 corpus ID。'
else
  echo 'DOCX_SET_UNCHANGED'
fi
)
```

只要移动了任一文档，后续 `corpus_id` 就必须设置为一个从未使用过的新值；
`install.sh` 会拒绝用变化后的文件集合覆盖旧 corpus ID。quarantine 要保留到
新 release 完成健康验收，永久删除命令见后文清理章节。

然后由操作员把当前 DOCX exact set 冻结为外置 canonical manifest。推荐输出到
已被 Git 忽略的 `corpus-manifests/`；私有文件名不进入仓库记录。打包前工作树
必须干净，且 manifest 与当前 docs 的路径、大小和 SHA256 必须完全一致。
分别保存三个镜像，禁止通过 glob 或归档内任意名称批量加载镜像。

```bash
(
set -euo pipefail

revision="$(git rev-parse HEAD)"
release_id="${revision:0:12}"
corpus_id='<本次-corpus-id>'
qdrant_ref='qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286'

test -z "$(git status --porcelain --untracked-files=all)"
[[ "${revision}" =~ ^[0-9a-f]{40}$ ]]
mkdir -p corpus-manifests
.venv/bin/python -m scripts.freeze_corpus_manifest freeze \
  --docs docs \
  --corpus-id "${corpus_id}" \
  --output "corpus-manifests/${corpus_id}.json"
export RELEASE_ID="${release_id}"
export CORPUS_MANIFEST="$(
  realpath "corpus-manifests/${corpus_id}.json"
)"
export RAG_APP_IMAGE="docx-rag:${release_id}"
export RAG_OCR_IMAGE="docx-rag-ocr:${release_id}"
export RAG_QDRANT_IMAGE="${qdrant_ref}"
bash deployment/package.sh
release_output="artifacts/releases/${release_id}-${corpus_id}"
(
  cd "${release_output}"
  sha256sum -c RELEASE_MANIFEST.sha256
)
printf 'RELEASE_ID=%s\nCORPUS_ID=%s\nRELEASE_OUTPUT=%s\n' \
  "${release_id}" "${corpus_id}" "${release_output}"
)
```

runtime 中的 `IMAGE_ARCHIVES.tsv` 固定为三行六列，字段依次为：归档相对路径、
镜像 tag、可移植的 `linux/amd64` platform manifest digest、provenance、config
digest、平台。第三列来自 `docker save` 完成后对归档内容的解析，不再记录保存前
daemon 私有的 `.Id`；第六列必须精确为 `linux/amd64`。`package.sh` 和
`verify-offline.sh` 都会逐张打开归档复核这六列。

在 WSL 的全新临时目录验证两个外层摘要、tar 路径和内部逐文件清单：

```bash
(
set -euo pipefail

release_id='<本次 12 位 release-id>'
corpus_id='<本次 corpus-id>'
release_output="$(pwd -P)/artifacts/releases/${release_id}-${corpus_id}"
verify_root="$(mktemp -d /tmp/rag-bundle-verify.XXXXXXXX)"
cleanup_verify_root() {
  if [[ -d "${verify_root}" && "${verify_root}" == /tmp/* ]]; then
    find -P "${verify_root}" -depth -delete
  fi
}
trap cleanup_verify_root EXIT

(
  cd "${release_output}"
  sha256sum -c RELEASE_MANIFEST.sha256
  sha256sum -c offline_bundle.py.sha256
)
.venv/bin/python "${release_output}/offline_bundle.py" \
  "${release_output}/rag-runtime-${release_id}.tar.gz" \
  "${release_output}/rag-runtime-${release_id}.tar.gz.sha256" \
  "${verify_root}" --top-level runtime
.venv/bin/python "${release_output}/offline_bundle.py" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz.sha256" \
  "${verify_root}" --top-level corpus
bash "${verify_root}/runtime/verify-offline.sh"
(
  cd "${verify_root}/corpus"
  sha256sum -c MANIFEST.sha256
)
echo 'LOCAL_DUAL_BUNDLE_VERIFY_OK'
)
```

上述过程会拒绝错误外层 SHA、路径穿越、链接/设备成员、重复成员、额外文件和
任一内部文件摘要漂移。验证完成后可删除该临时目录。

## 6. 上传到服务器

每次交付使用唯一目录
`/data/tyf/RAG/incoming/<release-id>-<corpus-id>/`，禁止把不同 release
平铺到 `incoming/`，也禁止复用共享 `incoming/extracted`。实际上传恰好七个
文件：两个归档、三个 `.sha256` sidecar、固定解包器和
`RELEASE_MANIFEST.sha256`。已验证的 Embedding/Reranker 共享模型资产不属于
这七个文件，不随 release 重传。

先由操作人员登录服务器并切换为 root，一次性复制下面整块。只为本次交付创建
`user4a:0700` 目录；禁止给 `/data`、`/data/tyf` 或 `/data/tyf/RAG` 执行
`chmod 777` 或递归放宽权限：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
release_id='<本次 12 位 release-id>'
corpus_id='<本次 corpus-id>'
upload_user='user4a'
upload_group="$(id -gn "${upload_user}")"
delivery="/data/tyf/RAG/incoming/${release_id}-${corpus_id}"

[[ "${release_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
[[ "${corpus_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
test ! -e "${delivery}"
install -d -o root -g "${upload_group}" -m 0710 \
  /data/tyf/RAG /data/tyf/RAG/incoming
runuser -u "${upload_user}" -- test -x /data
runuser -u "${upload_user}" -- test -x /data/tyf
runuser -u "${upload_user}" -- test -x /data/tyf/RAG
runuser -u "${upload_user}" -- test -x /data/tyf/RAG/incoming
install -d -o "${upload_user}" -g "${upload_group}" -m 0700 \
  "${delivery}"
printf 'UPLOAD_DIRECTORY=%s\n' "${delivery}"
)
```

然后在 WSL 仓库根目录一次性复制下面整块，只上传本次七个确定文件：

```bash
(
set -euo pipefail

repo_root="$(pwd -P)"
release_id='<本次 12 位 release-id>'
corpus_id='<本次 corpus-id>'
release_output="${repo_root}/artifacts/releases/${release_id}-${corpus_id}"
RAG_SERVER='<RAG 服务器 IP>'
remote="user4a@${RAG_SERVER}"
remote_delivery="/data/tyf/RAG/incoming/${release_id}-${corpus_id}"

test -d "${release_output}"
(
  cd "${release_output}"
  sha256sum -c RELEASE_MANIFEST.sha256
)
scp \
  "${release_output}/RELEASE_MANIFEST.sha256" \
  "${release_output}/offline_bundle.py" \
  "${release_output}/offline_bundle.py.sha256" \
  "${release_output}/rag-runtime-${release_id}.tar.gz" \
  "${release_output}/rag-runtime-${release_id}.tar.gz.sha256" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz.sha256" \
  "${remote}:${remote_delivery}/"
)
```

## 7. 服务器校验和安装

上传结束后重新登录服务器并切换为 root，把下面整块一次性复制执行。不要拆开
执行变量赋值；所有服务器路径均为绝对路径。该块先关闭上传窗口并将本次目录
收回为 `root:root`，再确认恰有七个上传文件；目录固定 0700、文件固定 0600，
随后校验和原子解包。解包器会
再次校验 sidecar 和内部 manifest，且目标存在时拒绝覆盖；不要使用
`tar --overwrite`。

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
expected_release_id='<本次 12 位 release-id>'
expected_corpus_id='<本次 corpus-id>'
project_root='/data/tyf/RAG'
delivery="${project_root}/incoming/${expected_release_id}-${expected_corpus_id}"
extract_root="${delivery}/extracted"
extract_stage=''

cleanup_extract_stage() {
  if [[ -n "${extract_stage}" && -d "${extract_stage}" \
    && "${extract_stage}" == "${delivery}/.extract."* ]]; then
    find -P "${extract_stage}" -depth -delete
  fi
}
trap cleanup_extract_stage EXIT

[[ "${expected_release_id}" \
  =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
[[ "${expected_corpus_id}" \
  =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
test -d "${delivery}"
test ! -L "${delivery}"
chown root:root "${delivery}"
chmod 0700 "${delivery}"
chown root:root "${project_root}" "${project_root}/incoming"
chmod 0700 "${project_root}" "${project_root}/incoming"

required_uploads=(
  'RELEASE_MANIFEST.sha256'
  'offline_bundle.py'
  'offline_bundle.py.sha256'
  "rag-runtime-${expected_release_id}.tar.gz"
  "rag-runtime-${expected_release_id}.tar.gz.sha256"
  "rag-corpus-${expected_corpus_id}.tar.gz"
  "rag-corpus-${expected_corpus_id}.tar.gz.sha256"
)
test "$(find -P "${delivery}" -mindepth 1 -maxdepth 1 \
  -printf '%f\n' | wc -l)" -eq 7
for filename in "${required_uploads[@]}"; do
  test -f "${delivery}/${filename}"
  test ! -L "${delivery}/${filename}"
done
if find -P "${delivery}" -mindepth 1 -maxdepth 1 \
  ! -type f -print -quit | grep -q .; then
  echo '交付目录包含非普通文件。' >&2
  exit 1
fi

chown -R root:root "${delivery}"
find -P "${delivery}" -mindepth 1 -maxdepth 1 \
  -type f -exec chmod 0600 {} +

docker_server_version="$(docker version --format '{{.Server.Version}}')"
if [[ "${docker_server_version}" != 29.* ]]; then
  echo "服务器必须使用 Docker Engine 29：${docker_server_version}" >&2
  exit 1
fi
docker_driver_status="$(docker info --format '{{json .DriverStatus}}')"
python3 -c '
import json
import sys

payload = json.load(sys.stdin)
expected = ["driver-type", "io.containerd.snapshotter.v1"]
raise SystemExit(0 if expected in payload else 1)
' <<< "${docker_driver_status}" \
  || { echo 'DOCKER_CONTAINERD_IMAGE_STORE_REQUIRED' >&2; exit 1; }

(
  cd "${delivery}"
  sha256sum -c RELEASE_MANIFEST.sha256
  sha256sum -c offline_bundle.py.sha256
  sha256sum -c \
    "rag-runtime-${expected_release_id}.tar.gz.sha256"
  sha256sum -c \
    "rag-corpus-${expected_corpus_id}.tar.gz.sha256"
)

test ! -e "${extract_root}"
extract_stage="$(mktemp -d "${delivery}/.extract.XXXXXXXX")"
python3 "${delivery}/offline_bundle.py" \
  "${delivery}/rag-runtime-${expected_release_id}.tar.gz" \
  "${delivery}/rag-runtime-${expected_release_id}.tar.gz.sha256" \
  "${extract_stage}" --top-level runtime
python3 "${delivery}/offline_bundle.py" \
  "${delivery}/rag-corpus-${expected_corpus_id}.tar.gz" \
  "${delivery}/rag-corpus-${expected_corpus_id}.tar.gz.sha256" \
  "${extract_stage}" --top-level corpus

runtime_stage="${extract_stage}/runtime"
corpus_stage="${extract_stage}/corpus"
bash "${runtime_stage}/verify-offline.sh"
(
  cd "${corpus_stage}"
  sha256sum -c MANIFEST.sha256
)
test "$(cat "${runtime_stage}/RELEASE_ID")" \
  = "${expected_release_id}"
revision="$(cat "${runtime_stage}/SOURCE_REVISION")"
[[ "${revision}" =~ ^[0-9a-f]{40}$ ]]
test "$(cat "${corpus_stage}/CORPUS_ID")" \
  = "${expected_corpus_id}"

mv -T "${extract_stage}" "${extract_root}"
extract_stage=''
runtime_dir="${extract_root}/runtime"
corpus_dir="${extract_root}/corpus"

install -d -m 0700 \
  "${project_root}" \
  "${project_root}/releases" \
  "${project_root}/shared/corpora" \
  "${project_root}/shared/env" \
  "${project_root}/data/state" \
  "${project_root}/data/qdrant" \
  "${project_root}/backups" \
  "${project_root}/logs"
bash "${runtime_dir}/install.sh" \
  "$(realpath -e "${runtime_dir}")" \
  "$(realpath -e "${corpus_dir}")"
chown -R 10001:10001 \
  "${project_root}/data/state" \
  "${project_root}/logs"
chmod 0700 \
  "${project_root}/data/state" \
  "${project_root}/data/qdrant" \
  "${project_root}/logs"

test -d "${project_root}/releases/${expected_release_id}"
test -d "${project_root}/shared/corpora/${expected_corpus_id}/docs"
printf 'RAG_RELEASE_INSTALL_OK release=%s revision=%s corpus=%s\n' \
  "${expected_release_id}" "${revision}" "${expected_corpus_id}"
)
```

`install.sh` 必须由 root 执行。它在复制前后分别校验 runtime 的逐文件摘要和
corpus 的 `MANIFEST.sha256`、`CORPUS_MANIFEST.json`，将 corpus staging
递归设为 `10001:10001`、目录 0700、文件 0400，再以不覆盖的原子 rename
发布；不再需要安装后手工修 corpus owner。release 已存在时，只有
`SOURCE_REVISION`、`MANIFEST.sha256` 和完整文件集合均相同才只读复用；
corpus 同样只允许完全一致时复用。因此同一 runtime 可以安装新 corpus，
相同 runtime/corpus 可幂等重跑，任一漂移、复制后篡改或发布竞态都会非零退出，
且不会删除既有目标或留下伪完整 staging。

候选配置始终在 release 外。候选目录固定为 0700；首次部署从新 release
样例安装候选文件，升级则从 active `rag.env` 复制到新 release 的候选文件。
下面整块自动区分首次部署和升级，并在两种情况下都先拒绝覆盖同名 candidate；
只编辑新 candidate，不直接编辑样例或 active env：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
release_id='<本次 12 位 release-id>'
project_root='/data/tyf/RAG'
release_dir="${project_root}/releases/${release_id}"
candidate_dir="${project_root}/shared/env/candidates"
candidate="${candidate_dir}/${release_id}.env"
active_env="${project_root}/shared/env/rag.env"

test -d "${release_dir}"
test -f "${release_dir}/.env.example"
test ! -L "${release_dir}/.env.example"
install -d -m 0700 "${candidate_dir}"
test ! -e "${candidate}"
test ! -L "${candidate}"

if [[ -f "${active_env}" && ! -L "${active_env}" ]]; then
  test "$(stat -c '%a' "${active_env}")" = 600
  install -m 0600 "${active_env}" "${candidate}"
elif [[ ! -e "${active_env}" && ! -L "${active_env}" ]]; then
  install -m 0600 "${release_dir}/.env.example" "${candidate}"
else
  echo 'active env 不是安全的 0600 普通文件。' >&2
  exit 1
fi

"${EDITOR:-vi}" "${candidate}"
printf 'CANDIDATE_ENV=%s\n' "${candidate}"
)
```

至少替换四个不同的随机令牌、三个模型端点数组、镜像 tag、
`RAG_DOCS_PATH`，并把 `RAG_RELEASE_REVISION` 设置为上面从
`SOURCE_REVISION` 读取的完整 `revision`。自动选择配置源不等于允许覆盖；
同一 release 的 candidate 已存在时必须先确认原因并停止。还必须保留
第 3 节已经验证的 Embedding/Reranker 端点；创建新 release 不要求也不允许把
共享模型资产混入七文件 delivery。配置还必须保留
`RAG_OCR_GPU_DEVICE_ID`：先在本次部署时重新执行 `nvidia-smi`，只填写已为 OCR
预留且当前可用的宿主物理 GPU ID；`.env.example` 中的 `0` 只是占位默认值，
不得依据旧截图直接沿用。配置还必须保留
`RAG_ACCESS_MODE=shared_corpus`：持有
query token 的所有用户都可检索全部 `active`/`official` 文档，当前没有
用户级、租户级或文档级权限；缺失该配置或填写 `permissioned` 会在启动时
失败。固定持久化路径应为：

```text
RAG_APP_IMAGE=docx-rag:<release-id>
RAG_OCR_IMAGE=docx-rag-ocr:<release-id>
RAG_QDRANT_IMAGE=rag-qdrant:<release-id>
RAG_OCR_GPU_DEVICE_ID=<本次为 OCR 预留的宿主 GPU ID>
RAG_STATE_PATH=/data/tyf/RAG/data/state
RAG_QDRANT_PATH=/data/tyf/RAG/data/qdrant
RAG_DOCS_PATH=/data/tyf/RAG/shared/corpora/<本次-corpus-id>/docs
RAG_ACCESS_MODE=shared_corpus
```

## 8. 启动与 GPU 冒烟

部署脚本要求 Docker Engine 29 的 containerd image store，按白名单依次使用
`docker load --platform linux/amd64` 加载三个归档，再以
`--no-build --pull never` 启动。六列 `IMAGE_ARCHIVES.tsv` 的第三列是可移植
platform manifest digest；加载后必须同时满足
`docker image inspect .Id == .Descriptor.digest == 第三列`，且平台等于第六列
`linux/amd64`。第五列 config digest 用于归档内身份复核，不得拿它替代第三列或
放宽加载后的比较。当前 provisional release 的默认路径只启动 app、OCR 和
Qdrant，不启动 worker；这时 `/ready=503` 是正确结果。

在第一条 `docker load` 前，脚本会把现场唯一分类为 fresh、installed 或
degraded。fresh 要求 active env、current、三个核心容器、worker 和 rollback
state 全不存在；installed 要求合法 active/current 与完整核心容器；degraded
要求合法 active/current、核心全无，并只允许不存在 worker 或保留 image 等于
旧 app image 的 worker。其他组合均为 invalid。fresh 若残留旧 rollback state
也会失败；installed/degraded 的旧 release 会在部署前和失败补偿前重新执行
`verify-offline.sh`。

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
release_id='<本次 12 位 release-id>'
release_dir="/data/tyf/RAG/releases/${release_id}"
candidate="/data/tyf/RAG/shared/env/candidates/${release_id}.env"

test -d "${release_dir}"
test -f "${candidate}"
test ! -L "${candidate}"
test "$(stat -c '%a' "${candidate}")" = 600
bash "${release_dir}/deploy.sh" "${candidate}"
)
```

deploy.sh 只接受候选文件；active rag.env 只能由 deploy.sh 成功后发布。
后续 Compose、备份和 rollback 继续读取固定的
`/data/tyf/RAG/shared/env/rag.env`。

Compose 的 Qdrant 端口 healthcheck 只用于启动顺序。deploy、rollback 和失败
补偿还会在 `rag-app` 容器内，以容器环境中的 Qdrant URL/API key 有界请求
`/readyz`；只有 HTTP 200 才允许提交 env/current。命令、日志和错误不输出
API key 或响应正文，Qdrant 也不发布宿主端口。

检查容器、应用存活和 OCR GPU 就绪：

```bash
(
set -euo pipefail

release_id='<本次 12 位 release-id>'
release_dir="/data/tyf/RAG/releases/${release_id}"
active_env='/data/tyf/RAG/shared/env/rag.env'

test "$(readlink -f /data/tyf/RAG/current)" = "${release_dir}"
docker compose \
  --env-file "${active_env}" \
  -f "${release_dir}/compose.yaml" ps
curl -fsS http://127.0.0.1:8088/live
docker exec rag-ocr python -c \
  "import urllib.request; print(urllib.request.urlopen(
  'http://127.0.0.1:8090/ready', timeout=3).read().decode())"
docker exec rag-ocr python -c \
  "import paddle; print(paddle.device.get_device()); \
  print(paddle.device.cuda.device_count())"
)
```

### 冒烟成功标准

- 默认路径只启动 `rag-app`、`rag-ocr` 和 `rag-qdrant`。
- `rag-worker` 必须不存在或保持停止。
- 应用 `/live` 必须返回 HTTP 200。
- Qdrant `/readyz` 必须返回 HTTP 200。
- OCR `/ready` 必须返回 HTTP 200，且 CUDA device count 必须大于 0。
- provisional 阶段 `/ready` 返回 HTTP 503 才是成功预期。
- 六份模型契约报告全部 `status=passed` 前，不得冻结检索参数或启动 `rag-worker`。
  六份分别对应 embedding、reranker 和四个 LLM。

OCR 必须报告 GPU 设备且 CUDA 设备数大于 0。runtime 已携带只读模型契约
验证器。以下整块从当前 app image 执行它；current runtime 只读挂载，
`rag-egress` 是唯一模型出口，令牌值只由 0600 的 active env 注入，命令行
只传 `--token-env` 变量名。每次执行都用 `mktemp` 创建只属于本次尝试的报告
目录，实际路径形如
`/data/tyf/RAG/logs/model-contract-<release-id>.<unique>/`；汇总也只读取该精确
目录，旧的 passed 报告不可能被本次验收复用。
报告由容器 UID 10001 写入，全部通过后收回为 root 只读。
报告不含令牌、问题或完整响应，也不要用 `tee` 把报告复制到终端：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
current_release="$(readlink -f -- "${project_root}/current")"
logs_root="${project_root}/logs"

test -d "${current_release}"
test "$(dirname "${current_release}")" = "${project_root}/releases"
test -f "${current_release}/RELEASE_ID"
test -f "${active_env}"
test ! -L "${active_env}"
test "$(stat -c '%a' "${active_env}")" = 600
test -d "${logs_root}"

release_id="$(cat "${current_release}/RELEASE_ID")"
[[ "${release_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
app_image="$(
  awk -F= '$1 == "RAG_APP_IMAGE" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
test -n "${app_image}"
report_dir="$(mktemp -d \
  "${logs_root}/model-contract-${release_id}.XXXXXXXX")"
chown 10001:10001 "${report_dir}"
chmod 0700 "${report_dir}"

run_model_contract() {
  local report_name="$1"
  shift
  docker run --rm \
    --network rag-egress \
    --env-file /data/tyf/RAG/shared/env/rag.env \
    --volume \
    /data/tyf/RAG/current/evaluation/runtime:/contract-runtime:ro \
    --volume "${report_dir}:/contract-logs" \
    --entrypoint /bin/sh \
    "${app_image}" -eu -c '
      report_name="$1"
      shift
      python /contract-runtime/scripts/verify_model_contracts.py "$@" \
        > "/contract-logs/${report_name}.json"
    ' contract-runner "${report_name}" "$@"
}

run_model_contract model-contract-embedding \
  embedding \
  --endpoint '<verified-embedding-url>' \
  --model Qwen3-Embedding-0.6B \
  --expected-revision '<verified-embedding-revision>' \
  --token-env RAG_EMBEDDING_API_TOKEN \
  --dimension 1024

run_model_contract model-contract-reranker \
  reranker \
  --endpoint '<verified-reranker-url>' \
  --model Qwen3-Reranker-0.6B \
  --expected-revision '<verified-reranker-revision>' \
  --token-env RAG_RERANKER_API_TOKEN

run_model_contract model-contract-llm-1 \
  llm \
  --endpoint '<verified-llm-url-1>' \
  --model Qwen/Qwen3-8B-AWQ \
  --expected-revision '<verified-llm-revision-1>' \
  --token-env RAG_LLM_API_TOKEN \
  --context-limit 8192 \
  --retrieval-config /app/deployment/config/retrieval.json \
  --llm-tokenizer /app/deployment/assets/tokenizers/llm/tokenizer.json

run_model_contract model-contract-llm-2 \
  llm \
  --endpoint '<verified-llm-url-2>' \
  --model Qwen/Qwen3-8B-AWQ \
  --expected-revision '<verified-llm-revision-2>' \
  --token-env RAG_LLM_API_TOKEN \
  --context-limit 8192 \
  --retrieval-config /app/deployment/config/retrieval.json \
  --llm-tokenizer /app/deployment/assets/tokenizers/llm/tokenizer.json

run_model_contract model-contract-llm-3 \
  llm \
  --endpoint '<verified-llm-url-3>' \
  --model Qwen/Qwen3-8B-AWQ \
  --expected-revision '<verified-llm-revision-3>' \
  --token-env RAG_LLM_API_TOKEN \
  --context-limit 8192 \
  --retrieval-config /app/deployment/config/retrieval.json \
  --llm-tokenizer /app/deployment/assets/tokenizers/llm/tokenizer.json

run_model_contract model-contract-llm-4 \
  llm \
  --endpoint '<verified-llm-url-4>' \
  --model Qwen/Qwen3-8B-AWQ \
  --expected-revision '<verified-llm-revision-4>' \
  --token-env RAG_LLM_API_TOKEN \
  --context-limit 8192 \
  --retrieval-config /app/deployment/config/retrieval.json \
  --llm-tokenizer /app/deployment/assets/tokenizers/llm/tokenizer.json

python3 - "${report_dir}" <<'PY'
import json
import sys
from pathlib import Path

report_dir = Path(sys.argv[1])
paths = sorted(report_dir.glob("model-contract-*.json"))
if len(paths) != 6:
    raise SystemExit("MODEL_CONTRACT_REPORT_COUNT_INVALID")
failed = False
for path in paths:
    payload = json.loads(path.read_text(encoding="utf-8"))
    status = payload.get("status")
    error_code = payload.get("error_code", "-")
    print(f"{path.name}: status={status} error_code={error_code}")
    failed = failed or status != "passed"
raise SystemExit(1 if failed else 0)
PY

chown -R root:root "${report_dir}"
find -P "${report_dir}" -mindepth 1 -maxdepth 1 \
  -type f -exec chmod 0400 {} +
chmod 0500 "${report_dir}"
printf 'MODEL_CONTRACT_REPORT_DIR=%s\n' "${report_dir}"
)
```

六份报告必须都满足 `status=passed`。整块只输出文件名、状态、稳定错误类别和
本次唯一报告目录，不打印完整 JSON；任一模型调用、JSON 解析、数量或状态检查
失败都会立即退出，不能继续启动 worker。

随后用管理令牌创建一次全量任务前，必须先完成检索参数冻结和模型 revision
核验，并在包含冻结配置的新 release 上显式启动 `index` profile。禁止在
服务器上直接修改 provisional 配置或绕过 worker 的严格索引门禁：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
release_dir="$(readlink -f -- "${project_root}/current")"

test -d "${release_dir}"
test "$(dirname "${release_dir}")" = "${project_root}/releases"
test -f "${active_env}"
test ! -L "${active_env}"
docker compose --profile index \
  --env-file "${active_env}" \
  -f "${release_dir}/compose.yaml" \
  up -d --no-build --pull never rag-worker
)
```

worker 稳定运行后才能创建全量任务；幂等键应包含本次 release 或操作日期：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
current_release="$(readlink -f -- "${project_root}/current")"
test -f "${active_env}"
test ! -L "${active_env}"
test -f "${current_release}/RELEASE_ID"
release_id="$(cat "${current_release}/RELEASE_ID")"
admin_token="$(
  awk -F= '$1 == "RAG_ADMIN_TOKEN" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
curl -fsS -X POST http://127.0.0.1:8088/api/index/jobs \
  -H "Authorization: Bearer ${admin_token}" \
  -H 'Content-Type: application/json' \
  --data "{\"idempotency_key\":\"initial-${release_id}\",\"kind\":\"full\"}"
)
```

轮询返回的 `job_id`：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
active_env='/data/tyf/RAG/shared/env/rag.env'
job_id='<上一步返回的 job_id>'
test -f "${active_env}"
test ! -L "${active_env}"
test -n "${job_id}"
admin_token="$(
  awk -F= '$1 == "RAG_ADMIN_TOKEN" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
ready_body="$(mktemp /tmp/rag-ready.XXXXXXXX)"
cleanup_ready_body() {
  rm -f -- "${ready_body}"
}
trap cleanup_ready_body EXIT

curl -fsS \
  -H "Authorization: Bearer ${admin_token}" \
  "http://127.0.0.1:8088/api/index/jobs/${job_id}"
curl -sS -o "${ready_body}" -w '%{http_code}\n' \
  http://127.0.0.1:8088/ready
cat -- "${ready_body}"
)
```

索引激活、manifest 一致、冻结检索参数存在且全部模型端点满足健康策略前，
`/ready` 返回 503 是正确结果。全部满足后才应返回 200。服务器不得为了得到
200 修改阈值、manifest 或健康策略。

## 9. 参数冻结后的生产验收

上一节只证明基础设施可启动。先由人工冻结集确定检索参数，把
`retrieval.json` 的状态和摘要按评审结果冻结并重建索引；不得仅修改状态字段。
随后准备人工核过的 `frozen-results.jsonl`，不能让同一 LLM 自评。运行时评测
会从活动 alias、SQLite manifest 和 Qdrant 现场重新导出可信证据，不接收自由
构造的证据清单：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
active_release="$(readlink -f -- "${project_root}/current")"
test -d "${active_release}"
test "$(dirname "${active_release}")" = "${project_root}/releases"
test -f "${active_env}"
test ! -L "${active_env}"
docs_path="$(
  awk -F= '$1 == "RAG_DOCS_PATH" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
[[ "${docs_path}" == "${project_root}/shared/corpora/"*/docs ]]
corpus_root="$(dirname "${docs_path}")"
test -d "${corpus_root}/evaluation"
app_image="$(
  awk -F= '$1 == "RAG_APP_IMAGE" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
docker run --rm --network rag-internal \
  --env-file "${active_env}" \
  --volume "${active_release}/evaluation/runtime:/opt/eval:ro" \
  --volume "${corpus_root}/evaluation:/opt/frozen:ro" \
  --volume "${project_root}/data/state:/state:ro" \
  --volume "${project_root}/logs:/evidence" \
  --env PYTHONPATH=/opt/eval \
  --entrypoint python "${app_image}" \
  /opt/eval/evaluation/evaluate.py \
  --dataset /opt/frozen/dataset.json \
  --results /evidence/frozen-results.jsonl \
  --qdrant-url http://rag-qdrant:6333 \
  --qdrant-alias rag-docx-active \
  --manifest-database /state/manifest.sqlite3 \
  --active-evidence-output /evidence/active-evidence.json
)
```

评测必须满足冻结门槛：Recall@20≥95%、rerank Recall@5≥90%、可答误拒≤10%、
不可答误答≤5%，引用 ID/原文匹配和提示注入防护均为 100%。

再执行 10 万 synthetic chunk Qdrant 基准。它使用独立随机 collection，
结束后按明确的 `--delete-after` 删除该基准 collection，不触碰活动 alias：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
active_release="$(readlink -f -- "${project_root}/current")"
test -d "${active_release}"
test "$(dirname "${active_release}")" = "${project_root}/releases"
test -f "${active_env}"
test ! -L "${active_env}"
app_image="$(
  awk -F= '$1 == "RAG_APP_IMAGE" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
docker run --rm --network rag-internal \
  --env-file "${active_env}" \
  --volume "${active_release}/evaluation/runtime:/opt/eval:ro" \
  --volume "${project_root}/logs:/evidence" \
  --env PYTHONPATH=/opt/eval \
  --entrypoint sh "${app_image}" -c \
  'python /opt/eval/scripts/benchmark_qdrant.py \
  --url http://rag-qdrant:6333 --api-key "$RAG_QDRANT_API_KEY" \
  --count 100000 --queries 200 --output /evidence/qdrant-100k.json \
  --delete-after'
)
```

最后执行 5 并发 30 分钟 chat 验收。查询令牌只从容器环境展开，命令和日志
不写令牌、问题或答案：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
active_release="$(readlink -f -- "${project_root}/current")"
test -d "${active_release}"
test "$(dirname "${active_release}")" = "${project_root}/releases"
test -f "${active_env}"
test ! -L "${active_env}"
docs_path="$(
  awk -F= '$1 == "RAG_DOCS_PATH" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
[[ "${docs_path}" == "${project_root}/shared/corpora/"*/docs ]]
corpus_root="$(dirname "${docs_path}")"
test -d "${corpus_root}/evaluation"
app_image="$(
  awk -F= '$1 == "RAG_APP_IMAGE" {
    count += 1
    sub(/^[^=]*=/, "")
    value = $0
  }
  END {
    if (count != 1 || value == "") {
      exit 1
    }
    print value
  }' "${active_env}"
)"
docker run --rm --network rag-internal \
  --env-file "${active_env}" \
  --volume "${active_release}/evaluation/runtime:/opt/eval:ro" \
  --volume "${corpus_root}/evaluation:/opt/frozen:ro" \
  --volume "${project_root}/data/state:/state:ro" \
  --volume "${project_root}/logs:/evidence" \
  --env PYTHONPATH=/opt/eval \
  --entrypoint sh "${app_image}" -c \
  'python /opt/eval/scripts/load_test_chat.py \
  --url http://rag-app:8088 --token "$RAG_QUERY_TOKEN" \
  --dataset /opt/frozen/dataset.json \
  --qdrant-url http://rag-qdrant:6333 \
  --qdrant-alias rag-docx-active \
  --manifest-database /state/manifest.sqlite3 \
  --concurrency 5 --duration-seconds 1800 \
  --output /evidence/chat-load.json'
)
```

验收要求错误率 `<1%`、召回加重排 p95≤2 秒、引用验证后的答案 p95≤60 秒，
并确认 10 分钟以上观察窗口内无 OOM、容器重启或 GPU 异常。三条命令任一
非零都不得标记生产就绪。

## 10. 备份

完整索引发布已创建并登记 Qdrant collection snapshot。版本升级前还必须使用
release 内固定的 `backup.sh` 备份 SQLite 与 Qdrant bind mount。禁止再使用
手工 `tar -czf` 流程：它无法可靠保证权限提升后的文件所有权、归档完整性和
失败后的原服务集合恢复。

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
active_release="$(readlink -f -- "${project_root}/current")"
test -d "${active_release}"
test "$(dirname "${active_release}")" = "${project_root}/releases"
test -f "${active_env}"
test ! -L "${active_env}"
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
bash "${active_release}/backup.sh" \
  "${backup_id}" \
  "${active_env}"
backup_dir="${project_root}/backups/${backup_id}"
(cd "${backup_dir}" && sha256sum -c MANIFEST.sha256)
stat -c '%U:%G %a %n' \
  "${backup_dir}/state.tar.gz" \
  "${backup_dir}/qdrant.tar.gz" \
  "${backup_dir}/MANIFEST.sha256"
)
```

脚本记录 app、worker、Qdrant 备份前的真实运行状态，停止并确认写入服务后，
通过 `sudo tar | gzip` 只提升源数据读取权限；最终文件归原调用用户所有且权限
为 0600。两个归档必须非空、gzip/tar 可读、成员路径和类型安全，并通过
`MANIFEST.sha256` 后才原子发布。成功或失败都会尝试恢复原运行集合；原来未
运行的 worker 不会被启动。恢复失败时脚本非零退出，但不会删除已经验证成功的
备份，也不会删除任何历史备份。

每个正式备份还包含进入 `MANIFEST.sha256` 的
`BACKUP_METADATA.json`：记录 UTC 创建时间、release ID、源码 revision、
app/OCR/Qdrant 实际 image ID、外置 active env 的 SHA256（不含 env 内容）、
两个归档 SHA；活动 manifest SQLite 可安全只读时还记录唯一活动 collection
和 manifest SHA，否则该字段明确为 `null`。app 恢复与 Qdrant 一样使用固定
30 次上限的有界轮询，不以单次 `/live` 请求判定恢复失败。

本轮 Agent 只实现并使用 fake 命令测试 `backup.sh`，没有在服务器执行备份、
恢复或回滚。操作人员实际部署后必须运行上述命令并保存退出码、manifest 校验、
所有权、权限和服务恢复证据，才能开始下一版部署。

## 11. 回滚

非首次部署会在 `/data/tyf/RAG/shared/env/rollback-images.env` 原子记录上一版
release 和三个实际镜像 ID。回滚脚本会重新执行旧 release 的
`verify-offline.sh`，校验 Compose、三个本地镜像、app/OCR OCI revision 和
Qdrant 固定身份；任一预检失败都不会修改共享 env、`current` 或回滚记录。
确认该文件存在后执行：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
test -f "${project_root}/current/rollback.sh"
test -f "${active_env}"
test ! -L "${active_env}"
bash /data/tyf/RAG/current/rollback.sh \
  /data/tyf/RAG/shared/env/rag.env
readlink -f "${project_root}/current"
curl -fsS http://127.0.0.1:8088/live
)
```

成功后共享 env 中的 `RAG_APP_IMAGE`、`RAG_OCR_IMAGE`、
`RAG_QDRANT_IMAGE` 和已有的 `RAG_RELEASE_REVISION` 会持久保存旧 release
选择；后续普通 Compose up/restart 不会切回新镜像。回滚前实际运行的 worker
会通过 `index` profile 使用旧 app 镜像恢复，原来未运行的 worker 不会被新增。
env 与 `current` 任一提交或最终复核失败时，脚本会补偿恢复原元数据。

回滚不删除或覆盖 SQLite、Qdrant、语料。
如果新版本已执行不兼容的数据迁移或索引变更，应按对应 IndexManifest 记录的
Qdrant snapshot 恢复；不得通过删除 bind mount 目录实现回滚。

## 12. 过期 DOCX 与旧失败发布清理

清理只能在第 8 节健康检查全部通过且 `current` 已精确指向新 release 后执行。
当前 `c2a69038d5f7` 从未成为健康 active release，不能作为回滚目标；但清理仍要
检查 active 和 rollback 记录，避免现场状态变化后误删。禁止使用
`docker image prune`，也禁止删除 `data/state`、`data/qdrant`、`backups`、
`logs` 或 `shared/model-services`。

在服务器以 root 一次性执行下面整块。若旧 corpus 仍被 active 或 rollback env
引用，脚本会保留它；若 c2 release 或镜像成为受保护回滚对象，脚本会拒绝或
跳过对应删除：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
new_release_id='<本次 12 位 release-id>'
new_corpus_id='<本次 corpus-id>'
old_release_id='c2a69038d5f7'
old_corpus_id='frozen-docx-v1'
active_env="${project_root}/shared/env/rag.env"
rollback_file="${project_root}/shared/env/rollback-images.env"
rollback_env=''

cleanup_rollback_env() {
  if [[ -n "${rollback_env}" && -f "${rollback_env}" \
    && "${rollback_env}" == "${project_root}/shared/env/.cleanup-rollback."* ]]; then
    rm -f -- "${rollback_env}"
  fi
}
trap cleanup_rollback_env EXIT

exact_value() {
  local file="$1"
  local key="$2"
  local count
  count="$(awk -F= -v key="${key}" \
    '$1 == key {count++} END {print count + 0}' "${file}")"
  test "${count}" = 1
  awk -F= -v key="${key}" '$1 == key {
    sub(/^[^=]*=/, "")
    print
  }' "${file}"
}

safe_delete_child() {
  local parent="$1"
  local name="$2"
  local parent_real
  local target
  local target_real
  [[ "${name}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]
  parent_real="$(realpath -e -- "${parent}")"
  target="${parent_real}/${name}"
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    return 0
  fi
  test -d "${target}"
  test ! -L "${target}"
  target_real="$(realpath -e -- "${target}")"
  test "$(dirname "${target_real}")" = "${parent_real}"
  rm -rf --one-file-system -- "${target_real}"
}

expected_current="${project_root}/releases/${new_release_id}"
current_release="$(readlink -f -- "${project_root}/current")"
test "${current_release}" = "${expected_current}"
test "$(cat "${current_release}/RELEASE_ID")" = "${new_release_id}"
bash "${current_release}/verify-offline.sh"
test -f "${active_env}"
test ! -L "${active_env}"
test "$(stat -c '%a' "${active_env}")" = 600

for service in rag-app rag-ocr rag-qdrant; do
  test "$(docker inspect --format '{{.State.Status}}' "${service}")" \
    = running
  test "$(docker inspect --format '{{.State.Health.Status}}' "${service}")" \
    = healthy
done
curl -fsS -o /dev/null http://127.0.0.1:8088/live

active_app="$(exact_value "${active_env}" RAG_APP_IMAGE)"
active_ocr="$(exact_value "${active_env}" RAG_OCR_IMAGE)"
active_qdrant="$(exact_value "${active_env}" RAG_QDRANT_IMAGE)"
active_docs="$(exact_value "${active_env}" RAG_DOCS_PATH)"
active_revision="$(exact_value "${active_env}" RAG_RELEASE_REVISION)"
test "${active_revision}" = "$(cat "${current_release}/SOURCE_REVISION")"
rollback_release=''
rollback_docs=''
rollback_image_ids=()

if [[ -e "${rollback_file}" || -L "${rollback_file}" ]]; then
  test -f "${rollback_file}"
  test ! -L "${rollback_file}"
  test "$(stat -c '%a' "${rollback_file}")" = 600
  rollback_release="$(exact_value \
    "${rollback_file}" ROLLBACK_RELEASE_DIR)"
  rollback_image_ids+=(
    "$(exact_value "${rollback_file}" ROLLBACK_APP_IMAGE)"
    "$(exact_value "${rollback_file}" ROLLBACK_OCR_IMAGE)"
    "$(exact_value "${rollback_file}" ROLLBACK_QDRANT_IMAGE)"
  )
  rollback_env="$(mktemp \
    "${project_root}/shared/env/.cleanup-rollback.XXXXXXXX")"
  exact_value "${rollback_file}" ROLLBACK_ENV_BASE64 \
    | base64 -d > "${rollback_env}"
  chmod 0600 "${rollback_env}"
  rollback_docs="$(exact_value "${rollback_env}" RAG_DOCS_PATH)"
fi

old_release="${project_root}/releases/${old_release_id}"
if [[ "${old_release}" == "${current_release}" \
  || ( -n "${rollback_release}" \
    && "${old_release}" == "${rollback_release}" ) ]]; then
  echo 'c2 release 是 active 或 rollback 目标，拒绝删除。' >&2
  exit 1
fi

obsolete_tags=(
  "docx-rag:${old_release_id}"
  "docx-rag-ocr:${old_release_id}"
  "rag-qdrant:${old_release_id}"
)
for obsolete_tag in "${obsolete_tags[@]}"; do
  case "${obsolete_tag}" in
    "${active_app}"|"${active_ocr}"|"${active_qdrant}")
      echo "c2 tag 仍是活动镜像，拒绝继续清理：${obsolete_tag}" >&2
      exit 1
      ;;
  esac
  if ! docker image inspect "${obsolete_tag}" >/dev/null 2>&1; then
    continue
  fi
  obsolete_id="$(docker image inspect \
    --format '{{.Id}}' "${obsolete_tag}")"
  protected=false
  for rollback_id in "${rollback_image_ids[@]}"; do
    if [[ "${obsolete_id}" == "${rollback_id}" ]]; then
      protected=true
    fi
  done
  if [[ "${protected}" == true ]]; then
    echo "保留 rollback 镜像：${obsolete_tag}"
  else
    docker image rm -- "${obsolete_tag}"
  fi
done

safe_delete_child "${project_root}/releases" "${old_release_id}"
safe_delete_child \
  "${project_root}/incoming" \
  "${old_release_id}-${old_corpus_id}"
safe_delete_child \
  "${project_root}/incoming" \
  "${new_release_id}-${new_corpus_id}"

old_corpus="${project_root}/shared/corpora/${old_corpus_id}"
if [[ "${active_docs}" == "${old_corpus}/docs" \
  || ( -n "${rollback_docs}" \
    && "${rollback_docs}" == "${old_corpus}/docs" ) ]]; then
  echo "保留 active/rollback corpus：${old_corpus}"
else
  safe_delete_child \
    "${project_root}/shared/corpora" "${old_corpus_id}"
fi

old_candidate="${project_root}/shared/env/candidates/${old_release_id}.env"
if [[ -e "${old_candidate}" || -L "${old_candidate}" ]]; then
  test -f "${old_candidate}"
  test ! -L "${old_candidate}"
  rm -f -- "${old_candidate}"
fi

echo 'OBSOLETE_C2_SERVER_ASSETS_REMOVED'
)
```

确认服务器清理成功后，才在 WSL 删除无效 c2 双包、旧单体 tar 和旧阻塞日志。
新 release 双包和 `artifacts/model-services/` 必须保留：

```bash
(
set -euo pipefail

repo_root="$(pwd -P)"
new_release_id='<本次 12 位 release-id>'
new_corpus_id='<本次 corpus-id>'
new_release="${repo_root}/artifacts/releases/${new_release_id}-${new_corpus_id}"
old_release="${repo_root}/artifacts/releases/c2a69038d5f7-frozen-docx-v1"
legacy_tar="${repo_root}/artifacts/rag-docx-offline-0.1.0-linux-amd64-20260727T055740Z.tar"

test -d "${new_release}"
test -d "${repo_root}/artifacts/model-services"
(
  cd "${new_release}"
  sha256sum -c RELEASE_MANIFEST.sha256
)
if [[ -e "${old_release}" || -L "${old_release}" ]]; then
  test -d "${old_release}"
  test ! -L "${old_release}"
  old_release_real="$(realpath -e -- "${old_release}")"
  test "$(dirname "${old_release_real}")" \
    = "${repo_root}/artifacts/releases"
  test "${old_release_real}" != "$(realpath -e -- "${new_release}")"
  rm -rf --one-file-system -- "${old_release_real}"
fi
rm -f -- "${legacy_tar}"
rm -f -- \
  "${repo_root}/artifacts/package-final.stderr.log" \
  "${repo_root}/artifacts/local-package-blocked-20260802.md"
echo 'OBSOLETE_C2_LOCAL_ASSETS_REMOVED'
)
```

若第 5 节输出过 `DOCX_QUARANTINE`，数据负责人再次确认新 corpus 已完成健康
验收后，才可将该精确目录永久删除；不得用 glob 清空所有 quarantine：

```bash
(
set -euo pipefail

repo_root="$(pwd -P)"
quarantine_root='<第 5 节实际输出的 DOCX_QUARANTINE 绝对路径>'
quarantine_parent="$(realpath -e \
  "${repo_root}/artifacts/docx-quarantine")"
test -d "${quarantine_root}"
test ! -L "${quarantine_root}"
quarantine_real="$(realpath -e -- "${quarantine_root}")"
test "$(dirname "${quarantine_real}")" = "${quarantine_parent}"
rm -rf --one-file-system -- "${quarantine_real}"
echo 'DOCX_QUARANTINE_REMOVED'
)
```

未来升级成功后，`rollback-images.env` 指向的上一版 release、三张镜像和旧 env
引用的 corpus 必须继续保留一代；只有再下一次升级改写 rollback 目标后，才可按
同样的 active/rollback 保护规则清理更老版本。

## 13. 停止条件

遇到以下任一情况立即停止当前依赖项，不继续部署：

- 外层 sidecar、内部 manifest、OCR 模型摘要或 frozen SHA 不一致；
- app/OCR 镜像不是 `linux/amd64`，或 OCI revision 不等于源码提交；
- 服务器需要 build、pull、安装软件或访问外网才能启动；
- `/data/tyf/RAG` 之外出现项目配置、语料或持久化数据；
- OCR 容器未识别 GPU、126 个媒体缺少成功或明确失败状态；
- 活动 alias、SQLite manifest、Qdrant pipeline metadata 不一致；
- 检索参数仍未冻结却试图放宽 `/ready`。

保留命令、退出码、SHA256、镜像 ID、job/trace ID 和阶段耗时作为交付证据；
不要记录原文、问题、答案或令牌。
