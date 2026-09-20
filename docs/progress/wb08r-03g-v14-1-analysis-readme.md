# WB08R-03 V14.1 C5 P0 本机分析包

本目录用于离线分析严格 V14.1 候选 C5 的新 P0。它含内部问题、回答、引用、授权目标
文档正文、完整 Trace 和候选日志，属于私有证据；不要提交 Git、发送到外部服务或公开分享。
目录不含账户密码、Token 或部署 `.env` 内容。

## 结论边界

- tested source：`543b4e3edc49345b9505725c44b8e1beaec43602`。
- 正式五题：F024、N031、显式来源、显式列名通过；危险时序失败。
- 新 P0：`P0_V141_FIELD_RESOLUTION_PROVIDER_HTTP_400`。
- 已证明旧检索归零问题未复现：目标文档结构完整，lexical 8、dense 22，范围/ACL/版本
  拒绝数均为 0，A1 有 25 条结构化证据，字段候选 4 个。
- 唯一后置 `query.interpret` 被 Provider 以 `HTTP_400 / INPUT_INVALID` 拒绝；没有 retry。
- 原始 400 响应体未被应用保存，精确不兼容字段为 `NOT_OBSERVED`。不要从
  `PROVIDER_INPUT_TOO_LARGE` 名称推断为真实 token 超限。
- 按用户停止规则，没有修改代码、创建第二候选或运行后续门禁。

## 目录说明

- `00_reference/`：V14.1/V14 任务书、验收用例和上轮 C4 对照证据。
- `01_trace/original/`：未经改写的最终私有证据归档。
- `01_trace/extracted/`：归档原始内容；正式批次在 `five-case-replay-r1/`。
- `01_trace/derived/`：由只读脚本生成的五题摘要和逐题完整 JSON。
- `01_trace/authorized-target-document-chunks.private.json`：获准目标文档 chunks。
- `01_trace/runner-permission-failure-trace.private.json`：第一次 runner 取证权限中断后恢复的
  F024 Trace，不属于产品 P0。
- `02_runtime/`：候选/生产/回滚身份、候选日志、version bundle、部署文件哈希和原 runner。
- `03_source_tested/`：测试提交的完整导出源码树与 tar.gz。
- `04_diff/`：审计基线到测试提交的完整 patch 和提交元数据。
- `05_reports/`：最终报告、P0 清单、manifest 和阶段状态副本。
- `tools/`：只读分析和全包哈希校验脚本。
- `SHA256SUMS.txt`：除自身外每个文件的 SHA-256。

## 建议复核顺序

1. 阅读 `05_reports/wb08r-03g-v14-1-p0-issues.md`。
2. 打开 `01_trace/derived/analysis-summary.json`，定位 `focus.unsafe_temporal`。
3. 对照 `01_trace/derived/cases/UNSAFE_TEMPORAL.full.json` 的事件顺序：
   `source_context` → `scoped_document_structure` → 三个 candidate ledger →
   `field_resolution` → `atom_grounding` → `generate` → `claim_publication` → `complete`。
4. 将危险时序题与 N031、显式来源、显式列名逐题 JSON 对比，确认基础文档/事实存在而
   只有模糊口语字段需要 `query.interpret`。
5. 查看 `01_trace/authorized-target-document-chunks.private.json`，人工核对表格 schema、
   输入项和 BEFORE/MUST 是否被源文明确证明。
6. 查看 `02_runtime/candidate-c5.private.log`，确认服务端记录 HTTP 400；注意日志没有原始
   400 body，因此不能继续推断具体 schema keyword。
7. 查看 `03_source_tested/tree/src/rag_app/application/answering/field_resolution.py` 和
   `03_source_tested/tree/src/rag_app/application/retrieval/service.py`，再结合 `04_diff` 复核
   本次真实走到的代码。

## 重新生成派生摘要

在安装了 Python 3.11+ 的环境中，从本目录执行：

```powershell
python .\tools\inspect_v141_c5_evidence.py `
  .\01_trace\extracted\wb08r-v141-c5-evidence\five-case-replay-r1\private-replay.ndjson `
  --output-dir .\01_trace\derived-regenerated
```

脚本要求输出目录不存在，不会修改原始 Trace。完成后可比较：

```powershell
Compare-Object `
  (Get-Content .\01_trace\derived\analysis-summary.json) `
  (Get-Content .\01_trace\derived-regenerated\analysis-summary.json)
```

## 完整性校验

```powershell
python .\tools\check_bundle.py .
```

预期检查除 `SHA256SUMS.txt` 自身外的全部文件；任何缺失、新增或哈希不一致都应失败。
原始归档 SHA-256 应为：

`108314029409120cb4212348cb950c2d077bef69d5b07a32b171d0e2cbc69e11`

正式 private replay SHA-256 应为：

`cba6f48c1b84326a5c534dfb6e789cf76abc7c3969317a3fd2f6d8ec9adf3bbf`

## 不应从本包得出的结论

- 4/5 不是 80% 产品准确率。
- HTTP 400 不等于已证明 token 超限。
- 当前证据不能证明某个具体 structured-output keyword 不受支持。
- 离线 144 passed 不证明真实 Provider 兼容或生产可用。
- 8289 回滚健康不代表 C5 通过；C5 已删除且未发布生产。
