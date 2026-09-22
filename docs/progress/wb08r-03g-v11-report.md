# WB08R-03G V11：物理表格事实与生成协议恢复冻结报告

V11 最终为 **FAILED / PAUSED**，`merge_allowed=false`，`production_publish_allowed=false`。本轮按最新审计结论实际完成了三项合同级修复：统一关闭正式生成、流式生成和复核的 thinking；以独立物理表格事实身份承载行名、表头和值；把补充生成改为逐 Atom、最多一次且不再依赖已有 accepted Claim。两份候选均只在专用 8289 服务完成真实 Preflight-16；第二轮仍为 0/16 非空答案，现按约定停止修改，不创建第三份候选，也不进入 A～F。

详细断点和下一次恢复条件见 [P0 清单](wb08r-03g-v11-p0-issues.md)。安全版本、测试、部署和清理由同目录 V11 manifest 固化。

## 实施结果

源码保留为两个独立提交：

- `06e8ce2a870c7c3c226463ff74ee4840cdb9f156`：新增独立 `PhysicalTableFact` 与 Atom 绑定，按真实表格坐标闭合行名、表头和值；正式生成、流式生成与关系复核统一使用显式 `disable_thinking` 策略；补充调度按 Atom 隔离，结构或响应协议失败可在许可来源内补充一次。
- `ef0c648ad83121fd3d1f8996a6681fea90331ba4`：针对 C1 的真实输入超预算，物理来源正文改为全请求只发送一次，事实注册表与 Atom 绑定分离；主生成得到零 Claim 的 `GENERATION_CLAIMS_INVALID` 时仍可进入一次逐 Atom 补充。

本轮没有删除 ACL、活动版本、来源身份、逐字引文、坐标/跨度、主体、阶段、模态、否定、数字和跨来源结构等硬边界。修改目标是让确定性的物理事实和补充控制流先正确工作，而不是放宽语义门禁。

## 只运行了与问答功能直接相关的验证

没有运行无关全库测试、UI、历史迁移或无关部署检查。

- `tests/application/answering`：242 passed。
- 生成证据包、阅读单元和 WB08R 自适应检索相关范围：58 passed。
- Provider、发送包、预算和关系复核相关范围：73 passed。
- 内部模型设置与模型装配：12 passed，1 个既有 warning。
- 物理事实与 attempt 定向范围：16 passed。
- 修改文件 Ruff、Ruff format、2 个修改源码模块 mypy、py_compile、`git diff --check`：通过。
- A～F：未运行，因为两轮 Preflight-16 均失败。

这些离线结果只证明合同和局部控制流按预期工作，不证明真实模型能稳定产出合规结构。

## 8289 两轮真实结果

固定运行身份：46 文档、2880 块、索引 `irev_0329a35ca9ea700133f7b309160118f0`、`Qwen/Qwen3-8B-AWQ`、输入/输出 6144/1536、rerank 24。两份候选都以 `rag-test-wanshitong:wb08r03f-bd00c56` 为冻结基镜像构建，运行树各 391 文件；冻结 version bundle 前 `business_requests=0`、`provider_calls=0`。

| 验收 | C1 `06e8ce2` | C2 `ef0c648` |
|---|---:|---:|
| 镜像 | `wb08r03g-v11-c1-06e8ce2` | `wb08r03g-v11-c2-ef0c648` |
| Preflight 请求 | 16 | 16 |
| 唯一 final | 16/16 | 16/16 |
| 非空且带支持的答案 | 0/16 | 0/16 |
| 实际 repair | 0 | 7 |
| gate | FAILED | FAILED |

C1 的 N031 与 F015 在生成发送前分别出现约 8919/11653 tokens 的输入预算失败，说明独立物理事实已经生成，但同一来源和事实关系在消息中重复展开。C2 压缩后，两类包均实际发送：N031 主/补充包约 5728/5731，F015 主/补充包约 5857/5847，均低于 6144；C1 的首个真实断点因此已经修复。

C2 共 16/16 唯一 final，但答案、引用、accepted Claim 和 published Claim 都是 0。runner 正常结束只表示执行完整；质量门仍是 FAILED。

### C2 已经证实修通的两条路径

1. **物理事实发送预算**：N031 三次均发送 14 个去重来源、11 个保护来源和 4 个物理事实；F015 三次均发送 12 个去重来源、11 个保护来源和 4 个物理事实。主包和补充包均为 `TRANSPORT_SENT`，不再是发送前预算阻断。
2. **零 Claim 补充调度**：N031 三次、F015 三次和 F034 一次都实际发生一次 repair，总计 7 次。`accepted=0` 和第一次响应协议失败不再阻止补充调用。

### C2 第一可观察失败

- N031 三次的主生成和补充生成都为 `GENERATION_CLAIMS_INVALID / RESPONSE_CONTRACT`；每次最终生成 Claim 数为 0。
- F015 三次的主生成和补充生成同样都为 `GENERATION_CLAIMS_INVALID / RESPONSE_CONTRACT`；每次最终生成 Claim 数为 0。
- F034 也在主生成和补充生成重复出现相同响应协议失败。

因此，当前主断点已经从“事实没闭合、包没发送、repair 接不到”移动到“真实模型在严格输出协议下连续两次没有产出可解析的 Claim 结构”。现有安全 trace 只保存了失败类别和调用结果，故具体是缺 JSON、字段缺失、类型错误还是 schema 约束不兼容均为 `NOT_OBSERVED`；本报告不补写未保存的原始响应。

### 其余真实样本

- N033 三次生成各 1 条 Claim，均以 `CLAIM_STAGE_UNSUPPORTED` 拒绝，未进入 repair。
- F013 三次生成各 1 条 Claim，均以 `CLAIM_MODALITY_MISMATCH` 拒绝，未进入 repair。
- F024 生成 1 条 Claim，以 `CLAIM_STAGE_UNSUPPORTED` 拒绝。
- N035 生成 1 条 Claim，原始原因 `CLAIM_QUERY_RELATION_UNDETERMINED`；关系复核约 26.27 秒后发生 HTTP transport transient，模型判定为 `NOT_OBSERVED`，未发布。
- A001 生成 1 条 Claim，以 `CLAIM_SOURCE_MISMATCH` / `CLAIM_SUPPORT_NOT_OWNED` 拒绝；关系复核的 4 项结果均为 contradicted / `MODEL_NOT_SUPPORTED`。

这些结果不支持删除阶段、模态或来源所有权门禁。它们与响应协议失败是不同类别，不能通过统一“放宽小门”处理。

## 调用和性能

C2 真实 Provider 计数：`embedding.query=16`、`reranking=16`、`query.interpret=9`、`query.rewrite=0`、`generation=25`；repair 7。

| C2 路径 | n | p50 | p95 |
|---|---:|---:|---:|
| 拒答 | 9 | 8491.05 ms | 33272.80 ms |
| repair | 7 | 36955.32 ms | 62659.82 ms |
| 全请求 | 16 | 16190.92 ms | 62659.82 ms |

answer 阶段 p50/p95 为 12961.52/57575.82 ms。模型 TTFT 和服务端排队时间没有完整观测。一次补充虽然已可达，但没有形成任何可发布答案，并显著增加尾延迟；不能通过继续增加补充次数或扩大 timeout 解决。

## 部署、回滚和清理

- 两份候选只部署到 60 服务器的 `127.0.0.1:8289`，候选健康检查均为 200。
- C2 失败后，8289 已精确回滚到 `rag-test-wanshitong:wb08r03f-bd00c56`，最终容器 ID `c21d6ade83a05b777a2fad2788ebf300af243cccdacd97bec7718c850deabb27`，image ID `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c`，健康 200。
- 生产容器 ID `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、生产 image ID `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306` 前后完全一致。60:8288 与 54:18288 最终只读健康检查均为 200。
- 没有修改 Qdrant、索引、业务数据、生产 Compose 或生产镜像。
- 60 与本地的两份 V11 候选镜像已按精确 tag 删除；未执行全局 prune。60 上只保留生产、8289 基线和既有 `wb08r02-9c3b029`；本地只保留生产和 8289 基线镜像。
- V11 构建 worktree、overlay、runner、传输包和远端临时证据已精确删除。原有 10 个未跟踪评测文件 SHA-256 与 V9 登记值逐项一致，未修改、未提交。

C1 私有证据位于 `C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03g-v11-20260919/c1-evidence.private.tar.gz`，SHA-256 为 `f4b465163c4ce855ce96ce1ab0274e127b39efa1323582cb669ea584e32ce3f0`。C2 私有证据位于 `C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03g-v11-20260920/c2-evidence.private.tar.gz`，SHA-256 为 `c5fd97378e53c228ba99fcab02765fff48e3865727c20320c832e7e287aa5697`。两者均未进入 Git。

## 冻结和恢复条件

V11 不创建第三份候选，不进入独立 Verification，不替换生产服务。当前代码保留用于审计，但不具备部署资格。

下一次若重新开启，应先作为独立的“模型输出协议”实验处理，而不是继续修改召回、物理事实闭合或增加门禁：安全记录结构失败分类，比较当前 `response_format`/schema 复杂度与更小合同，确认模型究竟在哪一层不兼容；只有固定回放先证明主生成能返回有效结构，并且一次补充能够改善而非重复失败，才获得新的 8289 候选资格。
