# 阶段 3 进度：Document IR、ParserPort 与旧 Element 兼容

## 状态

- Integration base：`c667d05d0e3e43eab2fe7060e16f2f3333586238`。
- Feature branch：`codex/p03-document-ir`。
- 实现提交：`081e77e`；测试提交：`7f813b0`；文档提交待本次提交后回填。
- Integration merge commit：待 `--no-ff` 合并后回填。
- `main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 与
  `Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` 只读。

## 实际修改

- 稳定 Document IR V1、SourceAnchor、文本/列表/表格/图片属性、ParseIssue/Report 和
  O(n) 全局不变量校验。
- 新增 ParsingPolicy 和真实安全上限，canonical policy 进入 index fingerprint。
- 注册 `legacy-docx-ir`，复用旧安全 Parser，验证 package/content type，并将图片写入
  BlobStore。
- 新增 IR 到旧 Element 反向适配与 CompatibilityReport。
- 新增默认不输出正文的 `scripts/dev.py inspect-document`。
- 合成 DOCX 全部在测试运行时创建；Git 不跟踪 DOCX/ZIP、数据库、索引、模型或 secret。

## Schema、公共接口与迁移

本阶段把 P01 provisional Document IR 骨架稳定为 schema version 1，并保留
`ParseSource`、`ParseResult`、`ParsePolicy` 别名和 `DocumentNode.text/structural_path`
兼容读取。BlobStore 新增幂等 `delete()`，只用于清理本次失败写入。

没有修改 HTTP/SDK schema、RuntimeSettings、SQLite/Qdrant schema、现有生产索引或 Query
API。旧表格结构损失、旧 metadata 和复杂节点损失都通过报告显式暴露。

## 已执行验证

起始 integration baseline：

```text
.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1070 passed, 75 deselected, 4 warnings in 182.85s
```

提交前最终验收：

```text
.venv/bin/python -m pytest -q tests/core/test_document_ir.py \
  tests/adapters/parsers tests/adapters/legacy/test_document_ir.py
17 passed in 0.24s

.venv/bin/python scripts/dev.py inspect-document \
  tests/fixtures/docx/simple-heading-paragraph.docx
document_hash_prefix=86b572970af8
parser=legacy-docx-ir@docx-parser-v3+ir-v1 nodes=2 issues=0
stories={'body': 2} coverage=1.000000

.venv/bin/python -m mypy --no-incremental src evaluation scripts
Success: no issues found in 169 source files

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1089 passed, 75 deselected, 4 warnings in 182.46s

.venv/bin/python scripts/dev.py smoke
58 passed, 1 warning in 1.67s
```

inspect-document 使用的 7 个运行时合成 DOCX 已在命令后删除，没有加入 Git。提交后和
合并后结果将在完成对应命令后回填，未执行项不写成通过。

## 外部服务与安全边界

应用测试默认离线，未调用 Jina、阿里、LLM、OCR、Qdrant 或真实 API Key，也未读取用户
私有文档。当前外部访问只有 GitHub origin 的 fetch/pull；push 状态待交付时回填。

## 决策与剩余风险

没有改变已接受 ADR、允许真实文档出网、引入付费服务、执行不可逆迁移、删除 legacy 或
合入 main，当前未触发决策暂停门。

P03 明确不提供复杂编号、cell provenance、完整 story、修订或批注正文；这些能力属于
P04。语义质量、真实 Provider 和生产 Qdrant 也未在本阶段验证。
