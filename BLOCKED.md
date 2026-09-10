# 当前 V3 Universal RAG 状态

当前源码状态以 [V3-05 功能收口报告](design/public/v3-05-functional-closure.md)
为权威入口；V3-04 的原始失败、扫描和 Live 质量证据仍保留在
[V3-04 验收报告](design/public/v3-04-unified-acceptance.md) 与
`artifacts/v3-04/`，没有被本轮结果覆盖。

| 状态项 | 当前状态 | 边界 |
| --- | --- | --- |
| `SOURCE_CODE_STATUS` | `PASS` | History/Trace 导出、Conversation、Feedback、singleflight、缓存/资源退役和离线资产闭环已实现并进入本地门禁 |
| `SOURCE_MERGE_READINESS` | `PASS` | 用户要求的源码与离线工程门禁通过后可进入隔离 V3-06；不代表生产发布已批准 |
| `PRODUCTION_RELEASE_READINESS` | `BLOCKED` | 精确候选的 OS 风险尚无责任人人工批准；模型不代签，也不运行生产发布 |
| `REAL_LLM_QUALITY` | `BLOCKED` | 本轮默认离线、无 Key，未获真实 Provider 授权，不能从 Mock/合同测试推断真实质量 |
| `OCR_LIVE_QUALITY` | `BLOCKED` | OCR 合同保留，但没有真实 OCR Provider 分母 |
| `DIAGRAM_RELATION_LIVE_QUALITY` | `BLOCKED` | 关系合同保留，但没有真实视觉 Provider 分母 |
| `OFFLINE_BUNDLE_STATUS` | `PASS` | Product 权威资产清单、同镜像离线包、篡改拒绝与隔离 App/Qdrant clean-room 均纳入强制验收 |

`PASS` 只覆盖对应行的源码、离线测试、浏览器或构建证据。它不代表真实模型、生产
代理、外部数据或人工风险处置已经完成。本阶段不需要重新解析、分块或嵌入；现有
Parser、Chunker 和索引策略身份保持不变。

当前工作流仍遵守以下边界：

- `main` 与 `Industry` 只读；不自动合并或更新。
- 远端推送开关为 `ALLOW_REMOTE_PUSH=false`，不得 push 或强推。
- V3-06 只允许在 V3-05 Review ZIP、SHA 和所有进入条件可核验后，于仓库外隔离
  worktree 对最新 `feature/universal-rag` 执行 `--no-ff` 合并并重跑门禁。
- 发布安全若因人工 OS 风险批准缺失保持 `BLOCKED`，不自动否定默认关闭未验证能力
  时的源码合并就绪；任何安全关键代码 `FAIL` 则必须停止。

历史 P11 质量/安全收尾仍可从
[原 P11 验收](release/p11-repair-acceptance.md) 和
[质量与安全收尾](docs/progress/p11-quality-security-closure.md) 查阅。旧候选
`d1f8d8d` / `05d6f9` 不再代表当前源码或镜像身份。
