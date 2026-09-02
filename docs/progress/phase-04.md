# 阶段 4 进度：DOCX OOXML Parser v4

## 状态

- Integration base：`1be6790f3fa1f1cf2451bf120195e5360a5fe694`。
- Feature branch：`codex/p04-docx-parser-v4`。
- 实现提交：`82d8968`；测试与固定语料提交：`b00240f`。
- 文档提交：本文件所在提交；最终分支 head 在远端复核后回填。
- Integration merge commit：待 `--no-ff` 合并后回填。
- `main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 与
  `Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` 保持只读。

## 实际修改

- 新增安全 OPC package、Part catalog、relationship graph、样式、自动编号、字段、修订、
  表格、Section/story、批注、notes、Drawing/VML、Text Box 与 issue 聚合实现。
- Document IR V1 以新增可选字段和 NodeKind 表示复杂 DOCX，公共 schema version 仍为 1。
- 新增 `V4DocumentIrToLegacyElementsAdapter`；复杂表格降级会明确报告损失。
- Registry 注册 `docx-ooxml-v4`，开发离线 Profile 切换到 v4；旧 Profile 默认 Parser 不变。
- 新增 20 个无业务信息的固定 DOCX、逐份 IR/report 快照、SHA-256 manifest 和生成器。
- 新增 support matrix、设计、安全说明，并更新 inspect/smoke 测试。

## Schema、公共接口与迁移

本阶段没有改变 HTTP/SDK、RuntimeSettings、SQLite/Qdrant schema 或现有索引。Document IR
新增 Section、Break、Note、Comment、Unsupported 节点类型，列表增加 ordinal/restart group，
ParseReport 增加 Part、relationship、media 和 revision 计数；所有变化保持 schema version 1
的加法兼容。Parser 版本改变会改变 index fingerprint，后续迁移必须建立新 revision。

## 已执行验证

起始 integration baseline：

```text
.venv/bin/python -m pytest -q tests/adapters/parsers \
  tests/core/test_document_ir.py tests/composition/test_registry.py \
  tests/test_dev_cli.py
31 passed in 0.24s

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1091 passed, 75 deselected, 4 warnings in 182.79s
```

开发期专项结果：

```text
.venv/bin/python -m pytest -q tests/adapters/parsers/docx \
  tests/core/test_document_ir.py
47 passed in 0.71s

.venv/bin/python -m mypy --no-incremental \
  src/rag_app/adapters/parsers/docx
Success: no issues found in 21 source files
```

提交前最终验收：

```text
.venv/bin/python scripts/dev.py inspect-document \
  tests/fixtures/docx_v4/03-numbering-restart-override.docx
document_hash_prefix=a808a4dd86a7
parser=docx-ooxml-v4@4.0.0 nodes=4 issues=0
stories={'body': 4} coverage=1.000000

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1132 passed, 75 deselected, 4 warnings in 177.92s

.venv/bin/python scripts/dev.py smoke
59 passed, 1 warning in 1.57s
```

提交后和合并后结果将在真实执行后回填，不预写通过数字。

## 外部服务与安全边界

代码和专项测试均默认离线，没有调用 Jina、阿里、LLM、OCR、Qdrant 或真实 API Key，也
没有读取用户私有文档。Provider 无关性测试证明 offline、Jina-only 和 Jina 主用/Qwen
standby 三种装配得到相同 canonical hash、node IDs 与 ParseReport。控制面只访问 GitHub
origin 完成 fetch/pull，push 与远端复核待提交后执行。

## 决策与剩余风险

没有修改既有 ADR、允许企业文档出网、引入付费服务、执行不可逆迁移、删除 legacy 或合入
main，因此未触发决策暂停门。

Word 的完整视觉分页、复杂浮动位置、caption 关联、图片项目符号渲染和未知 numFmt 仍按
support matrix 标记为 partial/metadata；Parser 不声称还原 Word 渲染结果。真实企业 DOCX、
语义检索质量、OCR、真实 Provider 和生产 Qdrant 均未在本阶段验证。
