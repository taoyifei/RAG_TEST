# DOCX RAG

这是一个面向离线单机部署的 DOCX 检索增强生成服务。核心目标是证据可追溯、
索引可增量更新、失败可恢复；前端仅是验收入口。V1 只解析 DOCX，不实现
PDF/PPT/Excel、Text2SQL、账号体系、LangChain 或 LlamaIndex。

## 主要组成

- `src/rag_app/`：安全 DOCX 解析、稳定 ID、SQLite 任务状态、Qdrant 索引、
  检索/重排/严格引用回答、API 与独立 PaddleOCR 客户端和服务。
- `evaluation/`：人工冻结集 schema、独立活动证据 manifest 校验和指标计算。
- `scripts/`：输入审计、负载/检索基准、发布安全扫描及 OCR 资产装配。
- `deployment/`：应用、Qdrant、单 GPU OCR 的离线 Compose 和恢复脚本。
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

默认 docstring 命令检查 `src/rag_app`、`evaluation`、`scripts` 全量 Python；
只有显式 `--changed` 才缩小到当前新增或修改文件。

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

资产装配见 `design/public/asset-assembly.md`，联网 WSL 到服务器回滚的
完整流程见 `design/public/offline-build-and-server-deployment.md`；
PaddleOCR 兼容入口保留在
`design/public/paddleocr-offline-deployment.md`。当前生产阻塞项以
`BLOCKED.md` 为准。
