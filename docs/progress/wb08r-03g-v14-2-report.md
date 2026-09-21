# WB08R-03G V14.2：03 收尾修复与真实诊断实施报告

## 最终结论

本轮状态为 **IMPLEMENTED / 03_DIAGNOSTIC_RUN_COMPLETE / NOT_PRODUCTION_RELEASED**。

源码已按最新方案完成小范围收敛，真实字段协议最终通过 5/5；同一候选版本已依次完成业务五题、Preflight-16 和 A～F。按照用户本轮补充的验收口径，真实问答阶段的目标是“完整跑通并定位问题”，不是要求准确率先达到原 03 阈值。因此 A～E 的质量 Gate 失败被完整保留为下一阶段问题，不再触发本轮算法补丁，也不阻止进入 04 的 Shadow Routing 准备。

这不等于问答质量已达标，更不等于可以替换生产。生产 `10.242.180.54:18288` 和 60:8288 未修改；当前候选只运行在 60 的 8289，等待单独授权。

## 版本与范围

- 分支：`codex/wb-08r-adaptive-rag`。
- 实现起点：`c7b005a614d92329875296d815d2101e5cae3844`。
- 实测源码：`6187be6f0bc9d822101ab5268b0b7eebd1905970`。
- 源码提交：`6187be6 fix(rag): 收敛字段解析执行链路`。
- 改动 25 个文件；没有重写 RAG、索引、`CompiledAnswerPlan`、物理表格事实、SourceScope 或限定证明体系。

## 按计划完成的代码修改

| 计划项 | 实现结果 |
|---|---|
| Atom 局部精确匹配 | `_default_resolution()` 只读取当前 Atom 的可信片段；完整问题仍用于身份、摘要和审计，不再用整句把其它 Atom 的字段词串进来 |
| 确定字段绕过模型 | 可唯一确定的输入、输出等字段直接形成 `EXACT`；后置模型只接收未解析 Atom |
| 单 Atom 完整小包 | 每次模型 HTTP 只处理 1 个 Atom，保留该 Atom 的全部候选；一个逻辑请求仍支持最多 4 个 Atom |
| 分项合并与失败隔离 | Atom 按序执行，共享 15 秒总 deadline；成功结果保留，单项失败不会清空已完成项；同一 Atom 不自动重试 |
| 字段专用预算 | 单次 read timeout 12 秒、阶段总预算 15 秒、输出上限 512 tokens；沿用全局并发池 |
| 状态合同说明 | 明确 `SUPPORTED_PARAPHRASE`、`RELATED_FIELD`、`AMBIGUOUS`、`NOT_FOUND` 的合法候选数量及“候选多不等于语义歧义” |
| HTTP 200 后诊断 | 本地合同拒绝时记录 Atom、状态、候选数量、q 长度、失败阶段/代码、usage、finish reason、正文长度和 hash；原文只进入既有私有诊断 |
| 能力边界 | 结构化合同声明单 HTTP 最大 1 Atom；应用逻辑上限仍为 4；`uniqueItems` 继续由本地验证器执行 |
| 历史相关测试 | 把旧 fixture 迁移到当前生成合同，保留“有效首轮后缓存提前命中”和逐 Atom 来源隔离的原业务断言 |

没有调整召回权重、业务同义词、索引内容、BEFORE/MUST 安全语义或生产模型服务。

## 离线验证

- 扩展的直接相关测试：`586 passed, 1 warning`。
- 最终改动范围定向回归：`93 passed, 1 warning`。
- Ruff check 和 Ruff format：23 个改动 Python 文件通过。
- mypy：408 个 source files 通过。
- `py_compile`、Compose 占位配置、`git diff --check` 通过。
- 未把业务真值、非法输出或漏答改成通过，未删除断言，未运行计划中的无关功能测试。

透明记录：执行过程中曾误触发一次更宽的 pytest 只读扫描，并在一个无关旧 snapshot 处停止；另有两次命令作用域过宽的 Ruff 只读扫描，报告的是既有无关 lint。三者均未写文件、未计入上述门禁、未据此修改无关代码。

## 真实字段协议资格

资格脚本按实际产品执行方式验证 `PARAPHRASE`、`AMBIGUOUS`、`NOT_FOUND`、确定性双 Atom 和 4×16 正常工作负载。旧 D3 失败记录未覆盖。

| 轮次 | 结果 | 处理 |
|---|---|---|
| 1 | 4/5；真实歧义被错误强选 | 修正计划内的歧义说明 |
| 2 | 4/5；`MAX_SHAPE` 的一个单 Atom 输出组合非法 | 修正计划内的单候选等义说明 |
| 3 | 5/5 | 最终资格通过 |

每轮 7 次真实 Provider 调用、0 retry；三轮共 21 次。最终轮中 `MAX_SHAPE` 以 4 个单 Atom 完整包执行，64 个候选没有截断；明确双问走本地 `EXACT`，后置模型调用为 0。

私有资格证据保留了三轮 safe/private 结果及一次有界 HTTP 合同诊断，不进入 Git。

## 8289 业务五题

部署后第一次发送 F024 时，Trace SQLite 权限中断，未形成 Trace；该请求未隐去。权限恢复后第一次完整五题因候选环境尚未执行内部模型配置，5/5 返回 `CONFIGURATION_REQUIRED`。随后按仓库正式命令执行模型配置两次并确认幂等，未修改算法；最终同版本五题结果如下：

| Case | 最终状态 |
|---|---|
| F024 | `INSUFFICIENT_EVIDENCE` |
| N031 | `ANSWERABLE` |
| 显式来源 | `ANSWERABLE` |
| 显式列名 | `ANSWERABLE` |
| 危险时序 | `ANSWERABLE`，实际执行一次后置 `query.interpret` |

五题阶段实际业务请求共 11 次：首次中断 1 次、配置缺失批次 5 次、最终批次 5 次。最终五题证明调用链可执行，不代表五题答案已经完成人工准确性验收。

## Preflight-16 与 A～F

以下各批全部实际执行，均为同一冻结版本，cache hit 为 0，没有对失败题重试或打补丁。`OBSERVED` 表示观测包完整，不表示业务质量通过。

| 批次 | 请求 | 终态分布 | Gate | 主要观察 |
|---|---:|---|---|---|
| Preflight-16 | 16 | 13 `ANSWERABLE` / 3 `INSUFFICIENT_EVIDENCE` | `NOT_REVIEWED` | 包完整；本轮未做人工语义复核 |
| A Evidence-Pack-12 | 12 | 11 / 1 | `FAILED` | 1 个 false refusal；answer/limited 0.8333 |
| B Core-Answer-16 | 16 | 13 / 2 / 1 `PROVIDER_UNAVAILABLE` | `FAILED` | F048、N059 的 answer/source 检查失败 |
| C Failed-24 | 24 | 17 / 6 / 1 `PROVIDER_UNAVAILABLE` | `FAILED` | 18 项具体检查失败；6 个 false refusal |
| D Natural-36 | 36 | 30 / 5 / 1 `AMBIGUOUS_NEEDS_CLARIFICATION` | `FAILED` | 6 个 answer/source 检查失败；2 个 false refusal |
| E Full-96 | 96 | 65 / 28 / 2 `PROVIDER_UNAVAILABLE` / 1 `AMBIGUOUS_NEEDS_CLARIFICATION` | `FAILED` | 7 个 false refusal；9 个错来源检查；F034 为无证据控制题 |
| F Concurrency-4 | 4 | 3 / 1 | `PASSED` | 4/4 唯一 final，无串会话观察缺口 |

Gate 业务请求合计 204；连同五题阶段，本轮业务请求总计 215。完整问题、回答、逐题观察及 Trace ID 已合并到 `all-requests.private.ndjson`。

## 完整 Trace 与重点归因

215 次业务请求中，214 次形成唯一 Trace，已全部导出 canonical JSON；首次权限中断的 F024 发生在 Trace 持久化之前，没有可补造的 Trace，单独记为观测缺口。证据包中每条合并记录都指向对应 `full-traces/trace_*.json`。

所有 `PROVIDER_UNAVAILABLE` 都是同一题 `WB08R-F-048`，在 B、C、E 正式集和 E 时延集共复现 4 次：

- 每次总时延约 38.56～38.98 秒；generation 约 33.54～33.89 秒。
- Provider 每次均返回 `SUCCESS / OK`，不是传输不可用；observed tokens 均为 2982。
- 模型每次返回 24 个条目；wire 接受 1 个，另外 23 个因引用未发送的 A2/A3/A4 而以 `UNKNOWN_ATOM` 拒绝。
- 最终进入 `SEMANTIC_REVIEW_DEADLINE_EXHAUSTED`，公共状态被映射为 `PROVIDER_UNAVAILABLE`，没有发布 Claim。

因此这是“模型 wire 输出越过 Atom 范围 + 语义复核 deadline + 终态分类”问题，不是 Provider 宕机。完整四条 Trace 和机器可读归因均已入包，本轮没有继续修复。

## 新问题修复配额

按保守口径，本轮在计划代码完成后处理了 2 类运行环境问题：Trace SQLite 挂载权限、候选内部模型配置未执行。两类均通过既有运维/配置路径恢复，没有形成第二套算法。

D3 的两轮提示词调整属于最新方案明确要求的协议资格收敛；A～E 的准确率、错来源和 false refusal 仅记录、不修改，因此不计作额外修复尝试。本轮没有达到“三类计划外新问题仍失败则登记 P0 并停止”的上限。

## 部署、生产与清理

- 8289 当前候选：`rag-test-wanshitong:wb08r03g-6187be6`，image ID `sha256:8da303c05f71e770be10f8845c0d336ad0feeb72839eb5a1baa6ae9de27cb9c1`。
- 候选容器：`67e240a2fa0845135b648342a09880ffd7d55abf274b149c7a84ee089b7e778b`；read-only rootfs、pids=256，live/ready 均 200。
- Product asset selfcheck：37 个文件、1,107,812 bytes，manifest SHA-256 `8c1f323bced7ef489962283b4a61cbaaabfe07ad835bd980751421d77cf55b3e4`。
- 60:8288 生产仍为 `rag-test-wanshitong:604ef63` / `sha256:97826be2208718be61d37921705f595c3c00566d57a2384ffc54ffdaf66380306`，容器 ID 与任务前一致，live/ready 200。
- 用户指定的 `10.242.180.54:18288` 从 54 主机和本机复核均为 live/ready 200；未修改。
- 精确删除两个停止的临时/回滚容器、临时候选镜像 `wb08r03g-v142-adde87a`、两个构建目录和三项临时归档/环境文件。
- 保留当前候选镜像及唯一回滚基线 `wb08r03f-bd00c56`；未删除 volume，未执行全局 prune。
- 本地 Docker 只观察到生产镜像和回滚基线，没有本轮过期候选镜像，因此未做破坏性删除。
- 原有 10 个未跟踪评测文件均保留，SHA-256 与历史清单逐项一致。

## 进入 04 的边界

按本轮最新口径，03 的“恢复执行并定位问题”目标已经完成，可以进入 04 Shadow Routing 的准备或实现；04 仍应保持影子、不可改变当前线上答案。

下列问题必须作为 04 期间的质量债务和生产替换门禁继续跟踪：F048 的 Atom 越界/终态误分类、A～E 的 false refusal、错来源、answer/source 失败，以及尚未进行的人工语义复核。没有用户单独授权前，不替换 54:18288/60:8288，不把当前结果宣称为准确率达标。
