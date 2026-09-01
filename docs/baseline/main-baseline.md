# main 起始基线

## Git 起点

| 引用 | 阶段 0 起始 SHA |
|---|---|
| `origin/main` | `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` |
| `origin/Industry` | `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` |
| `feature/universal-rag` | `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` |
| merge-base (`main`, `Industry`) | `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` |

`feature/universal-rag` 从 `origin/main` 创建，不以 Industry 为祖先增量；本阶段所有修改
先进入 `codex/p00-bootstrap`。

## Python 与依赖管理

- `pyproject.toml` 要求 Python `>=3.11,<3.12`；实测 `.venv` 为 Python 3.11.15。
- 构建后端为 `setuptools.build_meta`，固定 `setuptools==79.0.1`。
- 运行依赖在 `requirements.lock` / `requirements.runtime.lock` 固定；开发工具来自仓库
  `.venv`。实测 Ruff 0.16.0、mypy 2.3.0、pytest 9.1.1。
- 当前分发包没有安装进 `.venv`，但 `src` 源码树可导入；`doctor` 明确报告
  `project_import: source-tree`，不会伪称 editable install。

## 起始门禁实测

在 `origin/main` 起点创建阶段分支后、阶段功能实现前执行：

| 命令 | 起始结果 | 分类与处理 |
|---|---|---|
| `.venv/bin/python -m compileall -q src tests scripts evaluation` | 通过 | 无语法错误 |
| `.venv/bin/ruff check .` | 通过 | 无 lint 错误 |
| `.venv/bin/mypy --no-incremental src evaluation scripts` | 失败：1 项 | 模型契约探针缺 `question_profile`；用户授权后以 `a3a85b0` 修复，114 个源文件通过 |
| `.venv/bin/python scripts/check_google_docstrings.py` | 失败：22 段 | 5 个既有源码文件缺 `Args:`/`Returns:`；以 `9bcf8e0` 纯 docstring 修复，AST 行为不变 |
| `.venv/bin/python -m pytest -q` | `906 passed, 111 failed` | 用时 685.63 秒；失败分类如下 |

完整 pytest 的 111 项失败由两类组成：

- 63 项真实 Qdrant 集成测试访问 `127.0.0.1:6333` 并收到
  `Unexpected Response: 502 (Bad Gateway)`，属于当前环境服务不可用。涉及
  12 个测试文件，保留为手工/集成验证，不进入默认离线入口。
- 48 项为 main 中真实基线漂移：app-update 旧测试 25 项、非生成式客户端无效响应
  failover 10 项、runtime fixture 12 项、发布安全固定文件数 1 项。它们已分别由
  `45f08d2` 和 `cbf1d48` 修复；相关复验为 62 passed 和 26 passed。

修复后候选离线套件首次执行为 `926 passed, 1 failed`；唯一失败是阶段补丁写入了
错误层级的 prompt 指纹，随后已 amend 为 `actual_prompt_revision()` 的组合指纹并通过
21 个 pipeline/资产测试。最终统一入口结果记录在 `docs/progress/phase-00.md`。

## 最小 API、解析与检索集合

`python scripts/dev.py smoke` 使用纯合成数据，覆盖：

- `tests/test_health_api.py`：不启动外部服务的 API 健康合同；
- `tests/test_docx_parser.py`：合成 OOXML/DOCX 安全解析；
- `tests/test_chunker.py`：分块与来源跨度；
- `tests/test_rrf.py`、`tests/test_rerank_stage.py`：RRF 与重排边界；
- `tests/test_answer_guard.py`：引用与回答发布门禁；
- `tests/test_architecture_boundaries.py`：核心依赖和仓库敏感文件边界。

该集合不要求 Docker、Qdrant Server、模型 API、OCR 服务、真实密钥或真实文档。

## 现有模块图

```text
DOCX/OOXML -> parsers + OCR client -> chunking
                               |-> Qdrant index
                               `-> SQLite jobs/manifests/state

FastAPI/CLI -> runtime composition -> query_service
                               |-> retrieval (dense/BM25 -> RRF -> rerank)
                               |-> evidence -> answer/citations
                               `-> answer cache + Trace
```

## 已知耦合点

- `runtime.py` 同时组装 FastAPI、Qdrant、HTTP Provider、SQLite 状态和 Trace，是当前最主要
  composition coupling；通用 core 不能继续向这里反向依赖。
- DOCX 解析安全能力已较完整，但解析结果和 OOXML 细节仍直接存在于现有 parser 路径；
  后续 Document IR 必须在适配层外定义，不能把 OOXML 类型带入 core。
- `index/qdrant.py` 是具体 Store；SQLite manifest/job/control 分散在 `state`。后续 Store
  port 需要保留现有事务与来源身份校验。
- `clients/model_services.py`、`clients/llm.py` 和 `ocr/client.py` 含具体 HTTP schema；后续
  Provider port 应包住这些客户端，不让厂商 schema 进入 core。
- CLI 和 deployment 脚本直接读取现有配置路径；阶段 0 不改这些公共键或部署流程。

## 不进入通用主线的 Industry 目录/资产

- `deployment/industry/`
- `evaluation/industry/`
- `scripts/industry_bundle/`
- `scripts/industry_corpus/`
- `scripts/build_industry_bundle.py`
- `scripts/build_industry_app_update.py`
- `scripts/prepare_industry_corpus.py`
- `frontend/app-bearer.js`、`frontend/index-bearer.html`
- 所有 `tests/test_industry_*`、serving update/last-good/UI contract 工业验收资产

这些文件只作为只读证据存在于 Industry，不复制到 `feature/universal-rag`。
