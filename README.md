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
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/mypy --no-incremental src evaluation scripts
.venv/bin/python -m pytest -q
bash -n deployment/*.sh
docker compose --env-file deployment/.env.example \
  -f deployment/compose.yaml config -q
git diff --check
```

发布前先暂存候选文件，再运行：

```bash
git add -A
.venv/bin/python scripts/check_release_safety.py
```

资产装配见 `design/public/asset-assembly.md`，PaddleOCR 的完整离线流程见
`design/public/paddleocr-offline-deployment.md`。当前生产阻塞项以
`BLOCKED.md` 为准。
