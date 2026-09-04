# DOCX RAG

这是一个面向离线单机部署的 DOCX 检索增强生成产品。核心目标是证据可追溯、
索引可增量更新、失败可恢复；默认 `rag-app serve` 提供中文管理控制台和稳定
API。V1 只解析 DOCX，不实现
PDF/PPT/Excel、Text2SQL、账号体系、LangChain 或 LlamaIndex。

## 主要组成

- `src/rag_app/`：安全 DOCX 解析、稳定 ID、SQLite 任务状态、Qdrant 索引、
  检索/重排/严格引用回答、独立 Query Trace、API 与 PaddleOCR 客户端和服务。
- `evaluation/`：人工冻结集 schema、独立活动证据 manifest 校验和指标计算。
- `frontend/`：React/TypeScript 产品控制台、OpenAPI 生成类型与离线 Playwright。
- `scripts/`：输入审计、负载/检索基准、发布安全扫描及 OCR 资产装配。
- `deployment/product/`：Product Runtime 的最小 Compose 合同。
- `deployment/`：保留的 Industry/OCR 离线部署与恢复脚本；不再是默认入口。
- `design/public/`：不含业务语料的构建、发布和运维说明。

私有 DOCX、冻结题集、模型、tokenizer、wheels、镜像、结果和证据均由
`.gitignore` 隔离，不属于源码发布物。

## 本地校验

使用 Python 3.11 虚拟环境安装 `requirements.lock` 与开发工具后执行：

```bash
.venv/bin/python -m compileall -q src tests scripts evaluation
.venv/bin/ruff check .
.venv/bin/mypy --no-incremental src evaluation scripts
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_google_docstrings.py
.venv/bin/python scripts/check_google_docstrings.py --changed
bash -n deployment/*.sh
docker compose --env-file deployment/.env.example \
  -f deployment/compose.yaml config -q
git diff --check
```

## 首次启动

先在受控目录生成 0600 主密钥，并另行创建至少 16 个字符的 Bootstrap Token
文件；命令只显示路径和密钥指纹，不显示密钥值：

```bash
rag-app init-secrets --output /srv/rag-product/secrets/master-key
chmod 600 /srv/rag-product/secrets/admin-bootstrap-token
RAG_DATA_DIR=.data/product \
RAG_MASTER_KEY_FILE=/srv/rag-product/secrets/master-key \
RAG_ADMIN_BOOTSTRAP_TOKEN_FILE=/srv/rag-product/secrets/admin-bootstrap-token \
rag-app serve
```

浏览器打开 `http://127.0.0.1:8088/`，首次输入 Bootstrap Token 后会换取
HttpOnly 管理员会话；Provider 密钥不会进入浏览器存储。容器部署使用
`deployment/product/compose.yaml` 与同目录 `.env.example`。历史 Industry/OCR
栈仍保留在 `deployment/compose.yaml`，其中应用已显式调用弃用的
`legacy-serve`，只用于迁移期兼容。

前端统一门禁和离线启动方式见 `docs/development/frontend.md`。快速验证命令：

```bash
.venv/bin/python scripts/dev.py web-install-check
.venv/bin/python scripts/dev.py web-lint
.venv/bin/python scripts/dev.py web-typecheck
.venv/bin/python scripts/dev.py web-test
.venv/bin/python scripts/dev.py web-build
.venv/bin/python scripts/dev.py web-e2e \
  --profile configs/profiles/dev-offline.json
```

默认 docstring 命令检查 `src/rag_app`、`evaluation`、`scripts` 全量 Python；
只有显式 `--changed` 才缩小到当前新增或修改文件。

## 索引垃圾回收

`index-gc` 默认只生成无副作用计划，只有显式传入 `--apply` 才删除已证明不再被
alias、manifest、回滚窗口或任务引用的 collection、state 和 snapshot：

```bash
rag-app index-gc
rag-app index-gc --apply
```

存在 pending/running 索引任务或执行期间控制面发生漂移时，命令拒绝继续。
输出仅含稳定对象标识、原因和状态，不含正文、文件路径或配置内容。失败项可在
故障排除后重跑；collection 删除失败时不会先删除对应 state。

GC 在读取 pipeline、连接 Qdrant 或打开 SQLite 前先核对安装 wheel 与
`RAG_RELEASE_REVISION`。control 和 manifest 主库必须预先存在且不能是
symlink。规划会把主库与已提交 WAL 复制到临时隔离目录，再以
`mode=ro + query_only` 查询，并复核源 control、manifest、collection state
主库/WAL/SHM 的文件集与 SHA256 均未变化。`--apply` 会在删除前再次核对
Qdrant staging identity 和 state identity；state 主库、WAL、SHM 作为一个
逻辑集合删除，任一 sidecar symlink 或不完整删除都会返回失败状态。

section-aware chunking 的规则和定参边界见
`design/public/chunking-strategy.md`。在只读 DOCX 上执行四候选结构审计：

```bash
.venv/bin/python evaluation/chunking_ablation.py docs \
  --mode structural \
  --tokenizer deployment/assets/tokenizers/embedding/tokenizer.json \
  --pipeline deployment/config/pipeline.json \
  --corpus-policy deployment/config/corpus-policy.json
```

真实模型环境中的 retrieval 消融只允许读取 tuning 标签，并为每个候选创建独立
临时 collection/state；不得切 active alias。`tuning-document-map.json` 只能
包含文档键到相对路径，不能包含问题或 expected：

```bash
.venv/bin/python evaluation/chunking_ablation.py docs \
  --mode retrieval \
  --tokenizer deployment/assets/tokenizers/embedding/tokenizer.json \
  --pipeline deployment/config/pipeline.json \
  --corpus-policy deployment/config/corpus-policy.json \
  --retrieval-config deployment/config/retrieval.json \
  --dataset evaluation/frozen/questions.json \
  --document-map tuning-document-map.json \
  --qdrant-url "$RAG_QDRANT_URL" \
  --embedding-endpoint "$RAG_EMBEDDING_URL" \
  --reranker-endpoint "$RAG_RERANKER_URL"
```

当前 pipeline 和 retrieval 均保持 `provisional`。没有真实 embedding/reranker
tuning 结果时不得选择候选、声称准确率提高或读取 holdout expected。

发布安全检查必须使用临时 Git index，禁止教程或审查流程修改真实 index：

```bash
temporary_index="$(mktemp)"
rm -f "$temporary_index"
GIT_INDEX_FILE="$temporary_index" git read-tree HEAD
GIT_INDEX_FILE="$temporary_index" git add -A
GIT_INDEX_FILE="$temporary_index" \
  .venv/bin/python scripts/check_release_safety.py
rm -f "$temporary_index"
```

不得把含 `__pycache__/`、`.pyc` 或 `.pyo` 的审查 ZIP 当作发布源码包；
Git 候选、Docker build context 和发布源码清单也必须排除这些文件。

普通问答继续使用 query token；`/debug/`、`/api/admin/debug/chat` 和
`/api/admin/traces*` 只使用 admin token。Trace 使用
`RAG_TRACE_DATABASE` 独立 SQLite，普通模式由 `RAG_TRACE_MODE=SAFE` 或
`DIAGNOSTIC` 配置，query token 不能开启 FULL。内容边界、TTL、失败语义和
OTLP/Phoenix 预留见 `design/public/trace-observability.md`。

资产装配见 `design/public/asset-assembly.md`，联网 WSL 到服务器回滚的
完整流程见 `design/public/offline-build-and-server-deployment.md`；
PaddleOCR 兼容入口保留在
`design/public/paddleocr-offline-deployment.md`。当前生产阻塞项以
`BLOCKED.md` 为准。
