# WB08R-03 V14.1 C5 代码一致性检查

## 真实测试前结论

截至真实服务测试前，本工作树已经按
`V14_1_C5_协议接入收敛与03验收实施方案.md` 完成 A1、B、C、D1、D2
及 D3 资格脚本的代码闭合。当前结论仅表示“具备进入受控真实协议诊断的
代码条件”，不表示真实推理服务协议、五题质量或 03 完整验收已经通过。

本轮没有改动召回参数、业务同义词、物理表格事实定义、BEFORE/MUST
判定策略，没有增加自动 retry/repair，也没有更换模型。

## 版本与输入

- 代码起点：`de251f0324b82d8c406a81a616f405629e839c96`。
- 提交 A：`d074bde6790448dae9a91113371e6f3fffcbaa56`。
- 实施方案 SHA-256：
  `645e206a66e107f6cb5cd6cd82eed50f1d98c9964a92d8bc75818d7549df0c9f`。
- C5 证据 ZIP SHA-256：
  `2c117a5d27850b40e829bc4be0d2123b75009a27b8c8aee76bbc6d40148f2c6e`。

## 逐项一致性

| 工作包 | 当前实现 | 真实测试前状态 |
|---|---|---|
| A1 安全/私有失败观测 | `http_common.py` 记录有界状态、类型、code、param、body 长度/hash；`private_http_diagnostics.py` 只在显式 ACK、Git 外 0700/0600 目录记录有界请求和响应，写入失败不覆盖原异常 | 离线测试通过 |
| A2 服务身份固定 | 能力合同包含服务摘要、模型、模板、固定 mode、grammar 后端、资格证据和 deadline；已经 54→60 只读核验并写入安全身份记录 | 通过，待 D3 证明实际协议 |
| A3 受控 A/B | 资格脚本分离 `reconstructed-original` 与 `reconstructed-transferred`；前者逐字段重建 `de251f0` 的 v1 请求，后者只删除 `uniqueItems`，同一 fixture、同一 Adapter、160 输出预算、每阶段一次调用 | 脚本/差异测试通过，待真实执行 |
| B 单一字段合同 | `field_resolution_wire.py` 从一个合同生成请求、schema 和本地验证；拒绝重复 JSON 键/Atom/候选、跨 Atom/未知候选、错误状态组合和非唯一 q；使用 BUSINESS_QUERY digest/span，并验证可恢复的 original slice；模型不能签发 EXACT | D1 通过 |
| B 能力子集 | `structured_contract.py` 登记字段、Planner、Claim Wire、语义复核和关系复核五个家族；动态 enum hash 只作审计；`uniqueItems` 的 Provider/本地执行位置必须显式固定 | 通过 |
| B 输出预算 | 字段解析有独立 1280-token 输出预算；现场 Qwen tokenizer 对 1 Atom/4 候选、双 Atom、4 Atom×16 候选三种合法 JSON 的实测分别为 193、58、959 tokens，1280 不改变 6144 输入、8 秒 timeout 或 8192 总上下文 | tokenizer 门禁通过，待 D3 |
| C1 错误映射 | 只有明确上下文容量 code 映射 `ProviderInputTooLarge`；普通 400/422 为 `ProviderRequestRejected`，未知为 `REQUEST_REJECTED_UNKNOWN`；安全 Provider code 贯穿字段 Trace；400 不重试 | D2 通过 |
| C2 正交状态 | 字段结果使用 NOT_NEEDED/SUCCEEDED/REQUEST_REJECTED/TRANSPORT_FAILED/OUTPUT_INVALID；失败时不消费初始 AMBIGUOUS，Trace 将其标为 `PENDING_INITIAL` | 通过 |
| C3 终态/缓存 | 无可发布事实的字段系统失败映射现有系统失败终态；独立 EXACT 安全事实可以部分发布，缺失原因是 SYSTEM_DEPENDENCY_FAILED；系统失败不读写语义结果缓存；cache key 绑定合同/profile/执行状态 | 通过 |
| D1 本地合同 | 覆盖正例、重复 Atom、重复候选、跨 Atom、未知 ID、错误 q、重复键、非法组合、截断、额外字段及 uniqueItems 移交后的接收集约束 | 通过 |
| D2 实际 Adapter + MockTransport | 使用真实 `OpenAICompatibleChatAdapter`，核对最终 HTTP body、固定 mode、schema、输出预算、400 安全诊断和单次请求 | 通过 |
| D3 真实协议资格 | 五个安全合成 case，按字段身份而非 F1 顺序验真，同时检查状态、Atom、q 锚点、finish reason、token usage 和调用数 | 已实测，1/5 通过，资格失败并停线 |

## 现场只读身份与资格定义

- 推理服务：vLLM `0.19.1`，模型 `Qwen/Qwen3-8B-AWQ`，
  `max_model_len=8192`，AWQ，tensor parallel 2；服务命令没有显式
  structured-output backend 参数，因此能力记录使用
  `vllm-0.19.1:auto`，不把 auto 的逐请求实际选择当成已经验证。
- 8289 基线当前仍为 `rag-test-wanshitong:wb08r03f-bd00c56`，仅绑定
  `127.0.0.1:8289`；其应用配置实际使用 `response_format`。
- 生产仍为 `rag-test-wanshitong:604ef63`，容器、镜像和启动时间均与
  V14.1 收尾记录一致；本次只读核验没有修改生产。
- 安全身份记录：
  `docs/progress/wb08r-03g-v14-1-c5-runtime-identity.safe.json`，文件
  SHA-256 为
  `67bf43e71d4ea2ed6738194ce2cff5baf1645a6747cc92d352c186d8afeeeed6`。
  该值同时作为本轮能力合同的 service identity digest。
- D3 资格定义：
  `docs/progress/wb08r-03g-v14-1-c5-qualification-definition.safe.json`，
  文件 SHA-256 为
  `54da768d67c9587aadecba994f31fb5aecfeabca67fcaf1fb39d5821c2a7b119`。
  它固定五类合成题、请求计数和字段身份验真标准；真实结果另行保存，
  不用定义文件冒充已经通过的资格结果。

## 已执行的相关门禁

- 相关问答、协议、错误、缓存及配置测试：`565 passed, 1 deselected`。
- 固定 1280 输出预算及现场资格定义后的针对性回归：`55 passed`。
- Ruff：改动 Python 文件通过。
- Ruff format：改动 Python 文件通过。
- mypy：`407 source files`，无错误。
- Compose：使用非真实占位环境执行 `docker compose ... config --quiet` 通过。
- `git diff --check`：通过。
- 未运行与问答功能无关的测试。

## 计划外历史问题

以下问题在干净基线提交上同样复现，因此没有作为 C5 修复，也不影响
“C5 代码是否按任务书实现”的判断；最终报告继续单列，供人工决定后续范围。

1. `test_cache_hit_precedes_query_embedding_and_reranker`：首轮结果为
   `PROVIDER_UNAVAILABLE / GENERATION_OUTPUT_INVALID`，未写入可复用缓存，第二轮
   因而不能在 embedding/reranker 前命中。此前已在原始基线与提交 A 上复现。
2. `test_certified_excerpt_keeps_each_atom_source_scope`：期望 A2 为
   `SUPPORTED`，实际为 `MISSING`；本轮在提交 A 的独立干净 worktree 中复现。

两个问题均未通过删除断言、放宽门禁或修改非 C5 业务规则来制造通过。

## 进入真实测试的停线条件

只有在以下只读信息已经固定后才启动 8289 候选：实际服务 image/version、
模型/tokenizer、chat template、结构化输出 mode/grammar backend，以及部署
tokenizer 对三种合法输出形状的 token 上界。任何与任务书实现无关的新故障，
均停止继续修复，保存安全/私有 Trace、记录 P0、回滚并精确清理 8289 候选。

## 真实测试后的状态

上述条件满足后才执行了受控 A/B 与 D3：

- A/B 精确证明当前服务拒绝 `uniqueItems`，且只删除该 keyword 后接受相同重建请求；
  唯一性仍由本地合同严格验证。
- D3 的 `AMBIGUOUS` 通过；`PARAPHRASE` 超时，`NOT_FOUND` 输出状态组合非法，
  `TWO_ATOMS` 发生语义错配，`MAX_SHAPE` 在 6144 输入门禁前失败。
- 严格实现后出现新问题，故没有继续改产品代码、没有重试、没有执行业务五题，也没有
  构建或部署候选镜像。

最终状态、可观察边界和证据索引见：

- `docs/progress/wb08r-03g-v14-1-c5-d3-report.md`
- `docs/progress/wb08r-03g-v14-1-c5-d3-p0-issues.md`
- `docs/progress/wb08r-03g-v14-1-c5-d3-manifest.json`
