# Word 文档 RAG

这是一个可追溯、可增量更新、可恢复的 DOC/DOCX 检索增强生成产品。默认
`rag-app serve` 提供中文管理控制台和稳定 API；V1 使用 SQLite 保存产品状态，
使用正式 Qdrant Server 保存向量。Jina 与阿里云百炼凭据由管理员在页面配置，
不会写入镜像或浏览器存储。

## 五分钟容器路径

首次拉取基础镜像和构建时间不计入五分钟操作路径。默认只启动 `app` 与
`qdrant`，应用端口只绑定宿主 loopback：

```bash
cp .env.example .env
docker compose build app
docker compose run --rm --no-deps app \
  init-secrets --directory /run/rag-secrets
docker compose up -d
docker compose ps
```

初始化命令排他创建 0600 主密钥、Bootstrap Token、Qdrant API Key 与配置。
先把 `rag_secrets` 卷和主密钥单独备份，再用下面的命令在当前终端读取一次
Bootstrap Token：

```bash
docker compose run --rm --no-deps --entrypoint sh app \
  -c 'cat /run/rag-secrets/admin-bootstrap-token'
```

打开 `http://127.0.0.1:8088/`，输入 Bootstrap Token。随后在“模型服务”依次
保存并测试 Jina 与阿里云百炼连接，创建项目和知识库，激活主备检索方案，上传
DOC 或 DOCX 后即可问答。DOCX 保留结构化解析；旧版 DOC 以受限纯文本模式解析，
并明确记录结构降级。发送到远程 Provider 的只有管理员明确授权的查询、文档切片
和重排候选；页面会显示操作、Token 与切换用量。没有凭据时仍可完成本地 Exact/FTS
检索，但不得把它称为 Live Ready。

完整步骤见 `docs/public/quickstart.md`，部署与 TLS 见
`docs/public/deployment.md`，数据出网边界见
`docs/public/data-egress-and-cost.md`。

## 发布验收

Python 3.11 虚拟环境安装 `requirements.lock` 后，根目录只需这些主入口：

```bash
python scripts/dev.py check
python scripts/dev.py smoke
python scripts/dev.py product-check
python scripts/dev.py web-e2e
python scripts/release.py build
python scripts/release.py verify
python scripts/release.py acceptance
```

默认验证离线且无 Key。真实 Provider 验收只能由受保护的
`P11 Live Provider` 工作流手工触发，必须输入授权短语、预算并通过 Environment
审批。历史 Industry/OCR 七文件部署保留在 `deployment/`，全部标记为 Legacy，
不再是默认入口；迁移说明见 `docs/migration/from-industry.md`。

## 主要组成

- `src/rag_app/`：安全 DOC/DOCX 解析、产品状态、检索、引用、备份与 API。
- `frontend/`：React/TypeScript 中文控制台与 Playwright 测试。
- `compose.yaml` 与 `Dockerfile`：V1 默认的简单容器路径。
- `evaluation/`：独立评测 schema、证据校验和指标计算。
- `deployment/`：只为迁移保留的旧 Industry/OCR 部署资产。

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
