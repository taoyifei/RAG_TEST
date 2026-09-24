# WeKnora 核心阅读能力吸收：接入与 8289 验证记录

状态：`INTEGRATED_ON_Q1_CANDIDATE`。本轮完成来源回读、组级材料打包和最终生成请求接入，并在隔离的 8289 服务上实际跑通 F017。F017 的回答质量仍有 P0，见 [P0-F017-after-reader.md](P0-F017-after-reader.md)。本记录不宣称整套阻塞题或公开试用验收通过。

## 实现边界

- 从 `codex/wb08r-q1-algorithm-debt` 的 `ac6547c6b2c0da895a181b0b731736837b9af1c8` 建立独立分支；参考 WeKnora `v0.8.0` 的来源父级回读、连续内容组织思路，代码按本项目的 DocumentIR、SourceSpan 和 EvidenceGroup 合同实现，见 [UPSTREAM_PORTS.md](p00/UPSTREAM_PORTS.md)。
- 检索和重排的原有配置不变。新读取器消费固定的重排种子，从同一活动版本的权威来源节点和表格成员关系恢复完整结构；组级预算整体准入，候选生成证据直接来自选中的来源组。
- `RAG_CONTEXT_READER_MODE` 支持 `legacy|shadow|candidate`，默认 `legacy`。`shadow` 只对照材料；`candidate` 将新材料送入既有生成、物理表格事实、来源与 Claim 核验链。非 legacy 模式有独立 serving fingerprint。公开接口和生成模型参数未改。
- 8289 候选镜像由原专用镜像叠加本分支源码构成，沿用已有内网模型服务和隔离数据副本；没有重建活动索引或增加独立模型服务。生产 `54:18288` 的镜像、容器和数据没有切换。

## 真实 F017 材料链证据

候选源码 `d75c3b637777a5d5b3d190438223c8bce8347687`，真实 8289 Trace `trace_a00a8c9eaa359c96093a4de017873a6e`，状态 `ANSWERED`。记录只列结构、摘要与计数；不提交原文、凭据和完整私有 Trace。

| 层次 | 实测结果 |
|---|---|
| L1 重排后回读 | 12 个种子形成 13 个阅读组，9 个完整、4 个部分。F017 目标逻辑表格行恢复为 8 个成员，4 个重排种子；组的 `source_complete=true`，无缺失原因。 |
| L2 组级预算 | 9 个组入选，物化 32 个来源片段，硬拒绝来源数为 0；目标组的 8 个成员全部保留。 |
| L3 最终送模 | 私有发送捕获有 2 条真实生成请求。两条请求的目标组均通过 `source_group_covered`，最终包均将其列在 `complete_group_ids`，每条含 32 个 Evidence 且为 `TRANSPORT_SENT`，有发送 body 摘要。Trace 中首个自然准备包因输入预算拒绝、没有发送；随后既有重试发送包估算 5882 tokens，含 32 个来源别名和 18 个阅读单元，`removed_support_keys=0`。目标组 8/8 成员在该发送包中。 |
| L4 页面回答 | 通过 `54:8289/kb/` 登录并提交 F017，页面正常完成回答并显示 13 段引用；回答仍把疑似事件和确认后的报送方式合并成“电话和邮件、30 分钟”，没有分别完整说明两个阶段。 |

L3 证明新读取器的完整目标组实际进入了模型请求，且未被最终包再次裁散；它不等于 L4 正确。目标组终端标记在前一版候选中曾因空白 SourceSpan 被误判为部分组，已用一次针对性修正排除空白片段的认证映射，并在本次发送捕获中验证为完整组。

## 验证范围与留证

- 相关来源 adapter、读取器、组级预算、候选生成包、旧生成证据包与上下文闭合测试：`129 passed`。
- 修改文件的 Ruff check、Ruff format check、mypy 与 `git diff --check` 通过。一次额外的既有 Product runtime 测试在原分支及本分支均因迁移计数断言 `29`、现值 `36` 失败；未改动该无关断言，也未将其计入本轮通过。
- 本轮只执行 F017 专项真实问答；未执行方案建议的独立盲题、四并发和 P95 性能验收，因此不报告质量或性能提升。
- 私有证据保存在本机 `/home/jerry/.local/state/wb08r-context-reader-20260924/private-evidence/`，目录权限 `0700`、文件权限 `0600`。`trace-f017.private.json` 的 SHA-256 为 `fc94c191db74aacbb77a792c740baaf7059a5382912ed2646279349a92232e97`；`raw-generation-drafts.ndjson` 为 `fd1f6ea69dba5b60dcc1c8783289dc8ced049a942c95193a8b33451af9b50954`。

## 环境收尾

- 8289 已恢复原专用容器 `14c0f9a42c1479ef9c272ef4ff9f0c352c3855d292e9a5c9821f3021c0fdb591`、镜像 `rag-test-wanshitong:wb08r-f06-aee8755`，状态 healthy；生产 18290 应用容器 `83a2fc835c6beee2831d415b2d5336900a1c517e83ab04728dbe638388b1dce7` 保持 healthy。对外 `54:8289/kb/live` 和 `54:18288/kb/live` 均返回 200。
- 本次三版已停候选容器、三版候选镜像及对应服务器临时数据与构建目录已按精确身份删除；原 8289 镜像和数据保留。本机未构建或加载候选 Docker 镜像，本次传输的源码归档已清理；仅保留上述受限证据。
