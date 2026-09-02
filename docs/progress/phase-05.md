# 阶段 5 进度：结构化 Chunk、Token 打包与来源跨度

## 状态

- Integration base：`f0e4c04a78cd5571067ba13b30bae0a0dc6e77d1`。
- Feature branch：`codex/p05-structured-chunking`。
- 实现提交：`8cb4a56`；测试提交：`3f51228`；文档提交：`7f67da0`。
- 首次远端阶段 head：
  `origin/codex/p05-structured-chunking@7f67da0d5a9b8da2f1b3bf3648e0fe9a544ca67f`。
- Integration implementation merge commit：
  `0ba833111019e6ef97cc9200026344279eb50a6f`（`--no-ff`）。
- 远端实现状态：
  `origin/feature/universal-rag@0ba833111019e6ef97cc9200026344279eb50a6f`
  已由 `git ls-remote` 核对。
- `main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 与
  `Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` 保持只读。

## 实际修改

- Chunk Core Schema 升级为 V3，增加三视图、四类 SourceSpan、结构关系、token 元数据、
  required Provider slot 上限与聚合报告；旧 `.text` 是 citation 的只读兼容属性。
- 新增 IR-only `docx-structural-v3`，实现 section/run/atom、顺序 pack、语义拆分、完整句
  overlap、列表编号、复杂表格、nested table、notes、图片 metadata、家具策略和 neighbor。
- 新增确定性、本地 HuggingFace JSON 与保守估算 TokenCounter；无网络下载。
- Registry 和默认离线/hot-standby Profile 切换到 Parser v4 与 Chunker v3；fingerprint 覆盖
  policy、tokenizer identity 和 schema 3。
- 新增 V3 -> legacy 显式损失适配，并保持 legacy -> V3 基础字段兼容。
- 新增 `chunk-document` 与 `chunk-ablation` CLI；默认不打印正文，`--include-content` 才输出。
- 新增 Core、Chunker、TokenCounter、20 fixture、property、legacy、Profile、Registry、CLI 和
  Smoke 回归。

## Schema、公共接口与迁移

本阶段没有改变公共 HTTP/SDK、SQLite/Qdrant schema、现有 active collection 或生产 alias。
Canonical Chunk schema version 为 3，正文由 `text` 迁移为 `citation_text`；旧读取路径通过
显式 adapter 保留基础字段。复杂 SourceSpan、表格 parent/child、note refs 无法无损写回旧
payload 时返回 warning，必须在 P06 建立新 revision，不得塞进旧 active collection。

Chunker、policy、TokenCounter identity、required slot 限制和 schema 3 进入 index
fingerprint。纯文件重命名保持稳定 ID；内容、结构、policy 或 span 改变会产生新 ID。

## 已执行验证

起始前置检查：

```text
.venv/bin/python -m pytest -q tests/adapters/parsers/docx \
  tests/core/test_document_ir.py
47 passed in 0.91s
```

开发期专项与静态检查：

```text
.venv/bin/python -m pytest -q tests/adapters/chunkers \
  tests/core/test_chunk_models.py tests/adapters/legacy/test_contract_mapping.py \
  tests/composition/test_registry.py tests/composition/test_profiles.py \
  tests/composition/test_factory.py tests/test_dev_cli.py
72 passed in 2.52s

.venv/bin/mypy src/rag_app/adapters/chunkers src/rag_app/adapters/tokenizers \
  src/rag_app/adapters/legacy/contracts.py \
  src/rag_app/composition/chunking_cli.py src/rag_app/composition/factory.py \
  src/rag_app/composition/profiles.py src/rag_app/composition/registry.py \
  src/rag_app/core/models/chunk.py src/rag_app/core/ports/tokenizer.py
Success: no issues found in 24 source files

.venv/bin/ruff format --check <本阶段源文件和测试>
36 files already formatted

.venv/bin/ruff check <本阶段源文件和测试>
All checks passed!
```

固定验收命令：

```text
.venv/bin/python -m pytest -q tests/adapters/chunkers \
  tests/core/test_chunk_models.py
37 passed in 1.58s

.venv/bin/python scripts/dev.py chunk-ablation tests/fixtures/docx_v4 \
  --output /tmp/chunk-ablation
documents=20 candidates=3 chunk_runs=57 rejected=1
selection=provisional; freeze_in=P08

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1172 passed, 75 deselected, 4 warnings in 186.43s

.venv/bin/python scripts/dev.py smoke
60 passed, 1 warning in 1.85s
```

首次统一 `check` 在 pytest 前因新增内部函数缺少 Google docstring 小节而停止：编译、
Ruff 和 mypy 已通过，docstring 检查报告 `missing_google_sections=6`。随后只补齐这两处
docstring，复跑得到 `missing_google_sections=0` 和上述完整通过结果；没有删除、跳过或改写
失败用例。

合并提交 `0ba8331` 验收：

```text
.venv/bin/python -m pytest -q tests/adapters/chunkers \
  tests/core/test_chunk_models.py
37 passed in 1.58s

.venv/bin/python scripts/dev.py chunk-ablation tests/fixtures/docx_v4 \
  --output /tmp/chunk-ablation
documents=20 candidates=3 chunk_runs=57 rejected=1
selection=provisional; freeze_in=P08

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1172 passed, 75 deselected, 4 warnings in 186.19s

.venv/bin/python scripts/dev.py smoke
60 passed, 1 warning in 1.88s
```

## 结构消融与质量边界

候选固定为 `256/512/32`、`320/512/48`、`384/512/64`，每个候选复用同一份 IR
snapshot。输出只含结构报告，不调用真实 Embedding 或 Reranker。所有参数仍为 provisional，
本阶段不选择最佳候选；P08 才能在冻结 tuning/holdout 数据集与真实 Provider 下冻结。

## 外部服务与安全边界

实现、专项测试和结构消融默认离线，没有调用 Jina、阿里、LLM、OCR、Qdrant 或真实 API
Key，也没有读取用户私有文档。CLI 只读取仓库内无业务信息的合成 DOCX，默认不输出正文。
GitHub origin 的 fetch、pull、push 属于控制面访问；远端已核对阶段 head `7f67da0`、
integration implementation merge `0ba8331`，以及未变化的 `main`、`Industry` SHA。

## 决策与剩余风险

本阶段没有修改既有 ADR、允许企业文档出网、引入付费服务、执行不可逆迁移、删除 legacy、
改变公共 HTTP/SDK schema 或合入 main，因此未触发决策暂停门。

剩余风险包括：Token 参数仍未通过真实 Provider 与冻结检索数据集调优；HuggingFace 本地
tokenizer 的具体模型文件由部署方提供，未在仓库提交；P06 尚未持久化 V3 revision；真实
企业 DOCX、OCR、Jina/Qwen、生产 Qdrant 和语义检索质量均未在本阶段验证。
