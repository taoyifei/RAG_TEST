# 阶段 3 进度：Document IR、ParserPort 与旧 Element 兼容

## 状态

- Integration base：`c667d05d0e3e43eab2fe7060e16f2f3333586238`。
- Feature branch：`codex/p03-document-ir`。
- 实现提交：`081e77e`；测试提交：`7f813b0`；文档提交：`a554efc`。
- 兼容修复提交：`add7d35`，保留 P01 `legacy-docx` 受信任注册别名。
- Schema 输入兼容提交：`3a234f0`，将 P01 provisional 构造形状迁移为 V1。
- Integration implementation merge commit：
  `901d7a58fe6f8c5a84826a8c683ae375ff140e12`（`--no-ff`）。
- 远程实现状态：`origin/codex/p03-document-ir@a554efc` 和
  `origin/feature/universal-rag@901d7a5` 已由 `git ls-remote` 核对。
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
兼容读取；显式使用 `legacy-docx` 的旧 Profile 继续可用，新 Profile 使用
`legacy-docx-ir`。BlobStore 新增幂等 `delete()`，只用于清理本次失败写入。

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
合并后结果如下：

```text
.venv/bin/python -m pytest -q tests/core/test_document_ir.py \
  tests/adapters/parsers tests/adapters/legacy/test_document_ir.py
17 passed in 0.23s

.venv/bin/python scripts/dev.py inspect-document \
  tests/fixtures/docx/simple-heading-paragraph.docx
parser=legacy-docx-ir@docx-parser-v3+ir-v1 nodes=2 issues=0
stories={'body': 2} coverage=1.000000

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1089 passed, 75 deselected, 4 warnings in 184.28s

.venv/bin/python scripts/dev.py smoke
58 passed, 1 warning in 1.67s
```

最终门禁失败数为 0。`check` 明确跳过 75 个 `local_integration` 或 `live_provider`
测试，没有把它们写成通过。

最终兼容审计补回旧 Registry 名称后：

```text
.venv/bin/python -m pytest -q tests/composition/test_registry.py \
  tests/composition/test_profiles.py tests/core/test_document_ir.py \
  tests/adapters/parsers tests/adapters/legacy/test_document_ir.py
32 passed in 0.48s

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1090 passed, 75 deselected, 4 warnings in 181.58s
```

最终 Schema 审计增加旧 `DocumentNode`、`DocumentIR` 和 `ParseResult` 构造回归后：

```text
.venv/bin/python -m pytest -q tests/core/test_document_ir.py \
  tests/adapters/parsers tests/adapters/legacy/test_document_ir.py \
  tests/composition/test_registry.py tests/composition/test_profiles.py
33 passed in 0.23s

.venv/bin/python scripts/dev.py check
compileall / Ruff / mypy / Google docstrings passed
1091 passed, 75 deselected, 4 warnings in 184.36s
```

开发期测试真实暴露并修正了兼容 profile 期望（`69 passed, 1 failed`）、IR JSON/Locator
往返（`13 passed, 2 failed`）和 CLI 缺失 import（`31 passed, 2 failed`）。Google docstring
门禁曾报告 10 个 property 小节缺失；补齐后为 `missing_google_sections=0`。没有删除或跳过
失败用例。

## 外部服务与安全边界

应用测试默认离线，未调用 Jina、阿里、LLM、OCR、Qdrant 或真实 API Key，也未读取用户
私有文档。控制面访问了 GitHub origin 执行 fetch/pull/push，并已核对远程 refs。

## 决策与剩余风险

没有改变已接受 ADR、允许真实文档出网、引入付费服务、执行不可逆迁移、删除 legacy 或
合入 main，当前未触发决策暂停门。

P03 明确不提供复杂编号、cell provenance、完整 story、修订或批注正文；这些能力属于
P04。语义质量、真实 Provider 和生产 Qdrant 也未在本阶段验证。
