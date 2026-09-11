# V3-07 真正问答恢复状态

日期：2026-09-11

## 当前结论

V3-07 的正式答案现为 model-only：检索阶段把经过权限、Revision、来源和容量约束的
原文候选交给 Grounded 模型，模型输出再逐条经过对象、关系、数字、单位、否定、数量和
SourceSpan 校验。没有可用回答模型时，系统返回明确的配置、授权、预算或 Provider 状态，
不再由本地表格规则或 extractive renderer 拼出正式答案。

当前本机活动知识库此前已有 3 个真实 Jina + Qwen 代表性样本通过；本轮候选链又完成 4 个
匿名化的日常口语问题，覆盖上线准备、新项目首次沟通、提测说明和验收后关闭/归档职责。
这些问题均实际调用 Jina query embedding、Jina reranker 和 Qwen generation；需要解释的
问法还实际调用了 Qwen interpret/rewrite。已发布答案均为 `generation_mode=llm`、
`CLAIMS_VALIDATED`，引用可逐字回读，没有规则 fallback。另对其中一个原问执行了缓存复验，
答案与引用身份不变且 Provider usage 增量为 0。

这证明当前完整链路能真实工作，但不等于原 Prompt 中的私有 15/15、公开 80+ holdout 或
生产质量校准已经完成。此前基于本地 extractive renderer 的 15/15、44/44 和 B/C 数据只
保留为历史诊断，不能作为当前 model-only 版本的质量成绩。

## 已实现的问答合同

### 数据面、Profile 与授权

- 查询响应、History、Operational Trace 和管理员 UI 共用冻结的 `QueryDataPlane`，展示
  实际 Profile、Index Revision、fingerprint、Embedding、向量空间、Reranker、生成模型、
  语料授权和预算状态。
- Active Retrieval Profile、双向量槽覆盖、Jina query embedding/rerank 和 Qwen
  generation/interpret/rewrite 已接入正式 Product API；Profile、Revision 或向量身份不匹配
  时失败关闭，不静默切换为另一套语义。
- `CorpusAuthorizationManifest` 和 Retrieval Authorization 均绑定当前活动语料、Revision、
  Provider/model、operation、有效期与累计预算。语料或模型变化后授权失效，AI 不替管理员
  批准出网或扩大预算。

### 查询、检索与证据

- `QuerySemantics` 表达定义、目的、职责、责任角色、事实、枚举、数量、序号、流程、章节
  摘要和未知类型；原问题、实体、数字、单位、否定、序号、来源限定与时间/版本保持权威。
- Exact、FTS、结构通道、Dense、RRF、远程 Reranker 和邻居展开共用同一语义分析。重排池
  按通道族保留候选，避免重复 lexical/structural 结果挤掉真实 dense 候选。
- `retrieval_candidates`、模型可读候选、最终 claim 支持和已发布引用职责分离。模型前不再用
  本地答案规则清空候选；最终发布仍必须绑定真实、当前且有权限的引用。
- 正式引用缓存回读支持 canonical 长段落中的连续子范围，同时继续拒绝越界、错误节点、
  错误结构身份、不可引用范围和任何改字引用。
- 预算刚好耗尽时，同一授权、语料、Revision 和模型身份仍可先读取已有正式答案缓存；缓存
  未命中才返回 `BUDGET_BLOCKED`，不会为了判断缓存而构造可出网模型。过期、失配或拒绝的
  授权仍不能复用旧缓存。

### Grounded 生成、状态与 Trace

- Qwen 接收原问题、typed semantics 与统一的有界原文证据池，自行选择相关来源；一次初始
  生成，最多一次修复，不能按候选无限调用。
- 每个 claim 都校验 support ID、逐字 quote、对象/动作关系、数字、单位、否定、数量和完整
  来源组。不同表格行不能拼接借值，合法的同表格行或独立正文来源可以按分句绑定。
- 模型成功、证据不足、配置缺失、授权拒绝、预算阻断、Provider 不可用与内部失败分别投影
  为稳定状态；未发布权威答案时 History/Trace 不冒充 `ANSWERED`。
- SAFE/DIAGNOSTIC/FULL Trace 记录实际 data plane、语义来源、改写原因、候选选择、支持集、
  Provider call/usage 和最终状态，并保持各自的正文与容量边界。

## 当前执行证据

| 证据 | 结果 | 边界 |
| --- | --- | --- |
| 此前真实私有问答样本 | 3/3 `llm` + `CLAIMS_VALIDATED` | 早期 model-only 候选的代表性样本，不是 15/15 |
| 本轮口语化真实问答 | 4/4 `llm` + `CLAIMS_VALIDATED` | 候选链逐题运行、失败即停并修复；不等同于冻结 holdout |
| 本轮原问缓存复验 | 1/1 cache hit，答案 hash 与 4 个引用保持一致 | Provider usage 增量为 0 |
| Jina/Qwen 实际调用 | embedding、rerank、interpret/rewrite、generation 均有真实 usage | 只证明本机活动知识库 |
| 第一轮授权结束时累计用量 | Qwen 195,513/200,000；Jina 177,380/200,000 | 第一轮独立硬边界 |
| 本轮新增授权实际用量 | Qwen 33,888/200,000；Jina 15,617/200,000 | 新增独立额度；真实 Provider call 均成功，0 retry/429/failure |
| model-only 浏览器 E2E | 13 passed，3 个视口互斥 skip | 本地 Product/UI 合同 |
| 长引用与预算后缓存定向回归 | PASS | 连续子引用、安全反例、预算耗尽 hit/miss 和过期授权边界 |
| 当前部署基线 | App + Qdrant healthy，`/live`、`/ready` 200，产品资产自检通过 | 非生产部署 |

测试计数只记录实际执行结果。旧 renderer、旧离线 holdout、Mock、静态检查、容器健康和真实
Provider 问答是不同证据边界，不能相互替代。本轮真实问题和答案属于私有验收材料，Git 中只
保留匿名化类别、分母和状态，不写入原问、答案、文件名、Trace、数据库或凭据。

## 本轮失败定位与修复

- 缓存回读过去把 canonical 长段落中的合法连续子引用误判为 `INDEX_CORRUPT`；现在按
  Evidence 的 source 坐标投影并逐字复核，同时保留 node、anchor、结构和可引用性身份检查。
- 生成预算耗尽会让 serving identity 退化成 `model_required`，使已有正式答案缓存不可达；
  现在以不可出网的稳定 generation identity 先查缓存，miss 才投影预算阻断。
- 模型可读候选曾被少数 chunk 的多个 span 挤满；现在按 chunk round-robin 保留来源多样性，
  直接支持集的原顺序不变，并升级检索实现身份使旧证据缓存失效。
- 宽泛的句首对象启发式会把“需要准备”“上线前”等日常动作或时间上下文误判为对象偷换；
  现在只对明确实体主体执行核心对象一致性检查，部门、角色、系统、设备、数字和否定等安全
  反例仍失败关闭。
- Interpret 曾把口语里的时间上下文凭空提升成 source qualifier，导致正确候选被过滤；现在
  只有规则语义中已有的精确来源范围才可被模型保留，不能新增过滤范围。
- 多来源答案的一个分句曾混合不同来源组而触发 `CLAIM_SOURCE_MISMATCH`；Grounded prompt 与
  repair 现在要求一个 claim 绑定一个完整来源组，不同来源拆成独立 claim，校验器未放宽。

## ABCD 对照后的未完成项

- A1 要求的 15 条修改前逐阶段冻结账没有形成完整独立产物；现有矩阵可定位问题，但缺少每条
  Exact/FTS/Dense、候选淘汰、Provider/model/vector/call count 与终态的完整快照。
- D1 的当前 model-only 私有 15/15 尚未执行。历史 15/15 全部是
  `generation_mode=extractive`、Provider call 为 0，已失去当前质量证明力。
- D3 的当前 model-only 公开 80+ 数据集/holdout 尚未执行。历史 44/44 同样来自已删除的
  extractive 行为。
- D4 的当前 model-only B/C 正确性、引用精度和错误拒答对照尚未执行。按用户当前范围，纯性能
  矩阵、TTFC/CPU/RSS 和无关 P11/迁移门不补跑。
- 429、timeout、无效 JSON 与 claim 失败等状态已有源码/合成合同覆盖，但没有刻意制造真实
  Provider 故障；不会为了“live 全矩阵”主动消耗额度或制造外部失败。
- `/ready` 的远程生产质量校准仍为 `not_verified`。代表性成功样本证明链路可用，不证明广泛
  远程质量或生产容量。
- 旧 Review ZIP 和 merge receipt 绑定早期 extractive 候选，不能作为当前最终候选回执；
  最终 Git、镜像和运行态身份以本次交付核验为准。

## 明确排除或被后续指令覆盖的项目

- 原 Prompt 的本地结构化 renderer、`extractive_fallback` 和 Lane L 正式回答要求，已被后续
  “禁止对着表背答案、模型必须真实读取候选”的用户指令覆盖，不是待恢复功能。
- 完整性能矩阵、P11 replay、迁移矩阵、无关全仓测试、完整离线包和生产部署不属于当前问答
  恢复范围，不能为了补表格而机械运行。
- `main` 与 `Industry` 保持只读；只允许普通合入并推送 `feature/universal-rag`，禁止
  force-push。

## 交付门

最终候选必须从 clean HEAD 重建镜像，仅切换 App、保留 Qdrant 与三个持久卷；随后核对 OCI
revision、前端资产清单、`/live`、`/ready`、产品资产自检和实际 Git 远端引用。最终 SHA 与
运行态证据记录在交付结果中，不在本文硬编码一个会随文档提交变化的 revision。
