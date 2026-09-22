# WB08R-03G V12：问答链收敛与三轮真实候选冻结报告

V12 最终状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`production_publish_allowed=false`。本轮按《WB08R-03 问答阻塞审计与实施方案》
实施了统一 Wire、服务端来源绑定、一次批量语义复核、部分事实发布和表格字面证据
传输；三份候选均只在专用 8289 上测试。第三份候选仍出现确定性的严重事实错误，
触发用户规定的“三次修复即停”，因此不再创建第四份候选，也不进入 A～F。

详细根因、影响面与恢复条件见
[V12 P0 清单](wb08r-03g-v12-p0-issues.md)。版本、证据、部署和清理由同目录
V12 manifest 固化。

## 实施结果

本轮源码由三个按功能意图拆分并已推送的提交组成：

- `f9237c48ad40a75f469da2a7c95ec8367e07363d`：建立单一自然回答 Wire，
  用短 `EvidenceReadUnit` 编号让模型选择事实，由服务端恢复完整 Support；新增逐项
  绑定和一次批量语义复核，停用默认 repair，并允许独立正确事实发布。
- `eabda8b17ca3656901af856fb4f5945d009df9ed`：未闭合为
  `PhysicalTableFact` 的表格原文不再整体丢弃，而是以
  `literal_table_fragment` 进入发送包；无可发布事实时明确归为资料不足，不再伪装成
  Provider 故障；语义复核提示明确问句不是证据。
- `4d668b1613a7bd21edef7f9d5c495117befd0870`：把同一 Chunk、同一物理表格行
  的字面片段合并为一个阅读单元，同时保留所有 Support ID、来源跨度和“不可推断隐藏
  行列关系”标记；进一步禁止从问句带入时序、条件和义务强度。

没有删除 ACL、活动版本、来源身份、逐字引文、坐标/跨度、角色、否定、数字、单位、
条件或跨来源边界。C3 的表格合并只优化传输，不把字面片段升级为闭合表格事实。

## 只运行了与 03 问答直接相关的验证

没有运行全仓测试、UI、历史迁移或与问答无关的测试。

- 最终源码定向回归：132 passed，1 个既有 Starlette warning。
- 覆盖范围：生成证据包、问答服务回归、语义复核、证据绑定、Grounded Wire、
  Aliyun/OpenAI-compatible 生成与流式路径、自然答案、复核预算及 loopback 集成。
- 修改文件 Ruff、Ruff format、5 个相关源码文件 mypy、`git diff --check`：通过。
- 语义复核最坏预检估算为 3865 tokens，仍低于 4000 tokens 测试边界。
- C2、C3 新协议 P0～P3：每轮 4 类 × 3 次，共 12/12；HTTP、格式、绑定和合成
  语义检查均通过。
- A～F：未运行，因为 C3 Preflight-16 已确定失败。

这些结果只证明代码合同、发送预算和合成协议工作正常，不证明真实问题的事实质量。

## 三轮 8289 真实结果

三轮均绑定相同模型配置、活动索引和服务指纹；每轮 16/16 都产生唯一 final。

| 项目 | C1 | C2 | C3 |
|---|---:|---:|---:|
| 代码 | `f9237c4` | `eabda8b` | `4d668b1` |
| 镜像 ID 前缀 | `a061c20` | `e7012db` | `a239e36` |
| ANSWERABLE | 8 | 13 | 14 |
| INSUFFICIENT_EVIDENCE | 3 | 2 | 2 |
| PROVIDER_UNAVAILABLE | 5 | 0 | 0 |
| BUDGET_BLOCKED | 0 | 1 | 0 |
| Preflight 结果 | FAILED | FAILED | FAILED |

### C1：新链路可运行，但表格字面事实缺失和错误来源仍存在

C1 的真实链路不再被旧词面硬门直接接管，但 N031 三次均为
`PROVIDER_UNAVAILABLE`，F013 三次错误拒答，N033 三次虽有答案却未引用冻结期望来源；
F024 本应安全拒答却回答了其他文档中的内容。该轮共观察到 29 次 generation 调用，
没有 repair。

### C2：字面表格来源恢复，F024 在语义复核预留处超预算

C2 将 N031、N033、F015、F013 和 N035 恢复到带引用的 `ANSWERABLE`，F034 与
A001 正确归为资料不足；但 F024 在任何生成发送前因
`SEMANTIC_REVIEW_PREFLIGHT_BUDGET_EXCEEDED` 成为 `BUDGET_BLOCKED`。其最终保留的
8 个表格 Support 实际来自两个物理表格行，却按 8 个独立 JSON 阅读单元重复承担外壳
成本。该轮 packet 状态为 FAILED，共观察到 30 次 generation 调用，没有 repair。

C2 的人工抽查还发现：N031 把表格“输入”关系改写成“做快验前需要”，该时序并未由
来源陈述；F013、N033 已能发布有来源的安全部分。C2 因预算硬失败未获得继续 Gate 的
资格。

### C3：预算问题修通，但真实语义安全仍失败

C3 将同一物理行的片段合并后，F024 的生成和语义复核均实际发送，Preflight 不再有
预算阻断。16 条结果为 14 个 `ANSWERABLE` 和 2 个
`INSUFFICIENT_EVIDENCE`，packet 观测完整；Provider 计数为
`embedding.query=16`、`reranking=16`、`query.interpret=9`、
`query.rewrite=0`、`generation=32`，repair 为 0。

但 C3 暴露出两个确定性严重问题：

1. F024 的冻结行为为 `REFUSE`，实际为 `ANSWERABLE`；引用文档不是问题指定文档，
   `expected_source_in_citations=false`。生成后批量语义复核仍将该 Claim 判为可发布，
   形成错来源、模板正文越界和无依据事实。
2. N031 三次输出摘要完全一致，仍把“输入”关系改写成问句要求的前置时序义务。
   人工复核三次均记为 `UNSUPPORTED_HIGH_RISK_FACT_COUNT=1`。

正式复核摘要为 `v8_gate_status=FAILED`、`preflight_gate.status=FAILED`、
`v8_packet_status=OBSERVED`。F024 同时命中 `SAFE_REFUSAL`、
`WRONG_SOURCE_SEVERE_ERROR_COUNT`、`TEMPLATE_BODY_OVERREACH_COUNT` 和
`UNSUPPORTED_HIGH_RISK_FACT_COUNT`。其余 12 条在确定失败后没有继续做完整人工事实裁决，
因此保留为 `MISSING_FACT_REVIEW`，不能据此宣称通过。

## 固定身份与安全证据

C3 冻结身份如下：

- code SHA：`4d668b1613a7bd21edef7f9d5c495117befd0870`
- image ID：`sha256:a239e36db47d4c69a1be52ea97f5a7ab57f16e32c0d64bbf982f993846bf7178`
- runtime tree：`35f31fe9969eef31fa77c45cbba99e11ff599f3f65fa0d7d2aa028928d7ee10a`
- version bundle：`ed705a7eaf5b0b744bf6172e3a1603cc8fafd120d57ce1d57048b15a931ddf90`
- asset manifest：`906b4d1c643187660d4047489f4cc858aaade71079bf2487216e9a650ce32326`
- configuration：`356367b8ec7ffc0186d74c4fab6eed154c97c69e29a3c8b08619795482638802`
- serving fingerprint：`sha256:ff858c8bb682e1620db264984c09fc176e307d933699b95e7d4ceb9fc30a0707`
- 活动索引：`irev_0329a35ca9ea700133f7b309160118f0`
- Grounded Wire：`wb08r-grounded-wire-v9`
- Prepared Packet：`wb08r-prepared-packet-v3`
- Semantic Validation：`wb08r-semantic-validation-v3`
- Generation Evidence Pack：`wb08r-generation-evidence-v13`

冻结探针在任何业务请求前完成，`business_requests=0`、`provider_calls=0`。C3 P0～P3
安全摘要 SHA-256 为
`f3214193473410b7704785bb75c7de8b59eb8ad906eb64646cf3262297c756e5`；
Preflight reviewed summary SHA-256 为
`fd943071b680d098d40405f945e2394cf3fdba2ff5ccce9b8955634a9fa7e0bd`。

## 部署、回滚和生产边界

- 三份候选只部署到 60 服务器的 `127.0.0.1:8289`。
- C3 失败后，8289 已回滚到 `rag-test-wanshitong:wb08r03f-bd00c56`；最终容器
  ID 为 `a4b95d135a7a8a4222d2b7f465ad8220eeedac16edfce8f97545bec354f1d1a6`，
  image ID 为
  `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c`，
  `/live` 与 `/ready` 均为 200。
- 生产容器 ID
  `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、
  image ID
  `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`
  前后完全一致；60:8288 `/live` 为 200。
- 54:18288 从用户访问地址只读检查为 200；未操作该服务或镜像。
- 没有修改 Qdrant、索引、业务数据、生产 Compose 或生产镜像。

## 私有证据与清理

三轮私有正文和逐题证据均未进入 Git，已保存到：

- `C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03-v12-20260920/`
- C1：`wb08r03-v12-c1-evidence.private.tar.gz`，SHA-256
  `5a44a0dbdad1cfbbec2d981929d3a1d667d19644c158749348a0ed8a6158cff9`，112183 bytes。
- C2：`wb08r03-v12-c2-evidence.private.tar.gz`，SHA-256
  `08df00a03ad741a5292bde0aceaaf53e71200e1290eaf2707c8b87a00ac2b539`，81509 bytes。
- C3：`wb08r03-v12-c3-evidence.private.tar.gz`，SHA-256
  `2e1009ac7e05e6f257a6c53b37f97a4683f364251742bfbbb1168abc57ce9822`，85882 bytes。
- 三份 gzip 完整性检查均通过。

60 和本地的三份 V12 候选镜像已按精确 tag 删除；54、60 和本地的 V12 传输包、
临时 runner、冻结脚本、状态目录和三个 worktree 已精确删除。未执行全局 prune。
原有 10 个未跟踪评测文件逐项 SHA-256 与 V9 登记值一致，未修改、未提交。

## 冻结结论

V12 不进入 EvidencePack-12、CoreAnswer-16、Failed-24、Natural-36、Full-96 或
Concurrency-4，不替换生产，不开始 04。现有代码保留并推送用于审计，但不具备部署
资格。

下一次恢复不能继续追加提示词或让同一模型再次复核自身输出。最低恢复条件是：先把
问题中明确命名的来源约束变成服务端确定性所有权边界；再为表格关系建立可验证的关系
锚点，使“输入”只能发布为输入关系，不能从问句借出“之前/必须”；最后用 F024、N031
固定回放证明这两个严重反例为零，才允许创建新的 8289 候选。
