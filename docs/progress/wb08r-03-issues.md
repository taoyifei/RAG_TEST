# WB08R-03 阶段问题记录（2026-09-17）

## 状态与边界

- 实际起始提交：`6e979f624c639d4316657daa4f6585455090b528`，工作分支始终为 `codex/wb-08r-adaptive-rag`。
- 用户已要求暂停继续改代码。本阶段尚未完成真实功能 Gate，`merge_allowed` 保持 `false`；未合入 `feature/wanshitong`。
- 真实测试只使用 60 服务器候选服务 8289。18288 的生产镜像和服务未改动。当前 8289 镜像仍是 `0ebc6b9628a64cd910cb3c2d6f74e41cc4539273`，因此后续本地提交没有取得真实运行证据。
- 不在生产代码中加入评测问题、答案表、文档专用正则或 case ID 特判。一次针对成员归属问法的未提交规则已撤回，补丁保存在本机 `/tmp/wb08r03-uncommitted-membership-rule.patch`，未推送。

## 已观察到的问题

1. **逐原子证据归属曾丢失。** 多 Atom 初召回记录了 `AtomCandidateLink`，但原子 Grounding 曾对每个 Atom 重用合并后的全部候选。已改为按初召回锚点隔离，并保留该锚点的有界结构组成员；合成测试覆盖跨 Atom 串证据。此改动仅通过本地定向测试，尚未部署到 8289。
2. **复合答案的覆盖可能虚高。** 一条笼统 Claim 曾可同时标记多个 Atom；后续本地改动要求一条 Claim 只证明一个 Atom。候选旧版本的多问回答仍可漏掉时限、角色或条件，却呈现为可回答。最新改动尚未真实验收。
3. **导航与内容问题的边界过宽。** “需要哪些材料、哪些步骤”曾因“哪些材料”进入文档目录快路径，返回澄清。已在本地收窄“哪些材料”的导航判断并增加虚构问句测试；尚未部署。
4. **规划回退偏多。** 完整的旧候选 96 条中，22 条记录 `ADAPTIVE_PLAN_SCHEMA_FALLBACK`，其 Provider 调用均返回 `OK`。旧 Trace 无法区分 JSON、对象 Schema、来源限定和原问约束失败；现已加入不记录原文的回退原因诊断，待新候选验证。回退为单 Atom 时，复合问题的完整性尤其需要检查。
5. **Claim 校验失败原因不透明。** 旧候选 96 条中 30 条以 `CLAIM_NOT_SUPPORTED` 结束；旧 Trace 没有数值、对象、否定、来源或词汇支持等子原因。现已加入脱敏的拒绝原因计数，待新候选验证。不得为提高回答率放宽引用、数字或否定校验。
6. **结构类别有串答风险。** 当前候选的一个类别列举题引用了同一清单中相邻类别的行，并把它们表述为目标类别的成员。已有结构组目标锚定仍未充分防止这一情况；尚无通用修复通过真实验证。
7. **有限回答与自然度仍有缺口。** 旧候选的两个预期有限回答都变成拒答；一些复合回答包含笼统句、缺项或重复限定。来源命中和引用原文存在，并不等于答案的目标关系、完整性和自然度正确。
8. **端到端时延未达标。** 旧候选 Formal54、Natural 复合/多轮 18、Latency24 的 p95 分别为 23.80、24.87、23.46 秒，超过 DEEP p95 12 秒目标；DEEP 的 Planner 与 Generation 调用各约 10 秒，本地闭库纠错触发样本的 p95 小于 40 毫秒。不能以牺牲准确性换取时延。
9. **真实批量 Embedding 仍需独立确认。** 旧候选 Trace 可见同 slot 的批次大小 2/3 和 `PRIMARY_SELECTED`，但 Provider `call_count=0`；单元测试证明批量端口只调用一次，候选 Trace 尚不能证明一次真实 Provider Batch 请求。
10. **候选评测未完整收束。** `0ebc6b9` 的第二轮结果完成 Formal54 和 Natural 复合/多轮 18，仅完成 Latency24 的 11 条；下一条请求因“候选查询没有唯一 Final：[]”终止。按用户暂停要求不再重试。该轮共 83/96 条，不能称作完整验收。

## 真实候选证据

| 候选版本与范围 | 完成数 | ANSWERABLE | 预期来源出现在公开引用中 | p50 / p95 |
| --- | ---: | ---: | ---: | --- |
| `50ff77e` Formal54 | 54 | 28 | 28 | 11.18 / 23.80 秒 |
| `50ff77e` Natural 复合/多轮 18 | 18 | 10 | 7 | 13.47 / 24.87 秒 |
| `50ff77e` Latency24 | 24 | 17 | 15 | 11.13 / 23.46 秒 |
| `0ebc6b9` Formal54 | 54 | 26 | 待汇总 | 待汇总 |
| `0ebc6b9` Natural 复合/多轮 18 | 18 | 14 | 待汇总 | 待汇总 |
| `0ebc6b9` Latency24 | 11/24 | 9 | 不完整 | 不完整 |

上述 `ANSWERABLE` 是服务状态，不是人工准确率。旧版安全结果和 Trace 位于 `evaluation/wanshitong/v2/results/`；含问题与回答正文的私有审阅文件只保留本机，不提交。`0ebc6b9` 的未完成结果也只保留本机，不作为最终指标。

## 继续执行前的 Gate

1. 在 8289 部署与待验代码完全一致的提交，核对镜像源 SHA；不影响 18288。
2. 查明无唯一 `final` 的 SSE 请求，并确保流式策略与有限修复的边界成立。
3. 运行完整的 Formal54、Natural 复合/多轮子集、Latency24，记录逐 Atom 覆盖、来源和引用准确性、自然度、原文复制比、调用次数与时延。
4. 人工抽查单事实、定义、职责列表、流程、有限回答和拒答；优先看目标关系和限定条件是否被证据直接证明。
5. 仅在真实功能 Gate 通过后更新 WB08R-03 完成状态；本阶段不能自行合入基础分支。

## WB08R-03R 恢复记录（2026-09-17）

- 恢复起点为 `2892b7a5d3696bdfb6e9bb5ebdcac88ed03b08a2`；本次未回退、清理、合并或触碰 18288。旧评测结果仍为本机未跟踪文件。
- R0 修复提交 `4390fabce88bd3858abb64b8f6bcb3d313892940` 已推送。P09 流区分首协议事件、回答内容、协议空闲活动和带锁终态；runner 保留错误记录并完成剩余问题。
- R0 定向流式测试为 `24 passed`；新增显式冻结小样本选择测试后为 `25 passed`。`ruff check` 与 `git diff --check` 通过。
- 同时检查的旧 `tests/api/test_p09_e2e.py` 有两条既有失败：缓存命中断言和固定 stage 数量断言。在未修改的 `2892b7a` 独立归档上也复现，未修改断言或关闭测试。后续需按当前缓存与阶段协议分别处理。
- 60 服务器无法解析 Docker Hub，直接构建在基础镜像阶段失败。本机从 `4390fab` Git 归档构建候选，源码归档 SHA-256 为 `e03b29805c8359c94135a30c2f7f80b53233c85a7534223f1139b5ec770ee64a`；传输镜像归档 SHA-256 为 `16e778cc8938461eadc59aa6d9a738c87dc24f2a780669dfe70d027b0b29b7da`。
- 仅候选 8289 app 已替换为 `rag-test-wanshitong:wb08r03r-4390fab`，镜像 ID `sha256:ae0a4a15723bb532e93ae44d21343312156902772245001194d27c5961b0b05b`；容器健康且容器内 `SOURCE_REVISION` 与提交一致。
- Terminal-12 结果保存在本机 `evaluation/wanshitong/v2/results/wb08r03r-4390fab-terminal12.ndjson`：12/12 恰好一个终态，12/12 Final，首事件超时 0，静默结束 0，Transport Retry 0。正文审阅文件仅留本机。
- Terminal-12 中 3 条预期可回答样本返回 `INSUFFICIENT_EVIDENCE`；复合样本最大总时延为 19.23 秒。R0 只证明终态合同，未证明回答准确率、逐原子归属或 R6 时延达标。R1～R6 和完整 96 仍待完成。
- R1 使用当前 8289 容器环境和虚构最小 Schema 探测同一 vLLM：`enable_thinking=false` 可用；`response_format=json_schema` 与 `structured_outputs.json` 返回有效对象；`guided_json` 响应无效。四次探测分别约 556、353、353、494 毫秒，未打印 URL、凭据、Prompt 或响应正文。显式配置将选 `response_format`，候选环境更新与 Planner/Claim 实际接入留给后续候选镜像。
- R1 在模型配置、兼容 Adapter 与通用 JSON 提取器中建立单一协议选择；无生产查询时轮询协议，也无格式错误后二次完整模型调用。配置升级路径已允许已锁定候选只更新已探测的 no-thinking/结构化模式。相关定向测试 `45 passed`，`ruff check` 与 `git diff --check` 通过。
- R2 代码将 Planner 输出缩为 `intent/needs_clarification/clarification_question/atoms(fragment,target,relation,answer_shape)`；`standalone_query` 固定保留原问，来源和硬约束由服务端提取，非法 JSON/Schema/片段/新增字面值整份回退。短问本身不再触发 Planner。`QUERY_PLAN_SCHEMA_REVISION` 已升至 v2，Trace 记录 Schema 模式与摘要。
- R2 发现现有 QueryAnalyzer 的数字模式在中文字符紧邻阿拉伯数字时漏提（例如“超过5天”）；最小 Planner 的校验和约束构建直接扫描原文数字、单位、日期、版本，避免依赖模型补足。这个问题仍可能影响主链其他 QueryAnalyzer 消费点，需在 R3 审计，但不能用某个评测问题的专用规则修复。
- R2 相关定向测试 `38 passed`；Planner-24 真实 Gate 尚未运行。当前 8289 仍为 R0 的 `4390fab`，不能用其结果评价 R2。
- R2 首次真实 Planner-24 使用精确镜像 `171ce3e996b274014a19f9e8758d773534f04142`（镜像 ID `sha256:e2e717714e25d40e66245f05a89060c07a758371fb6c5d7c943ee84dca0a20ab`，候选端口 8289），`response_format` 与 no-thinking 均已在容器内确认。24/24 留有记录，Planner 22 次，回退 5 次（3 次 Atom 校验、2 次 Provider 超时），p50 3.45 秒，p95 5.008 秒；R2 Gate 未通过。候选镜像部署只重建 `wanshitong-wb08r01-app`；18288 未动。
- 脱敏诊断显示 3 次 Atom 校验回退中，1 次模型只引用上轮 Context、遗漏当前问句；另 2 次两个独立原子共用同一个连续原文片段。后者符合最小 Payload 合同，现已删除额外的“片段必须唯一”限制，并在 Prompt 明确允许共用。前者仍要求当前问句覆盖，不能为降低回退而放宽。两种受支持的结构化输出协议对两个长问题均触发约 5 秒 Provider 超时，因此切换协议本身不能解决。
- R2 第二次真实 Planner-24 使用精确镜像 `ca91d91ea538579ee83114698de7fe6a5d8d4083`（镜像 ID `sha256:62b01151292a70c6a9c6d37702bd81f0983c3002bb6ec010ffdf0614a3730f94`）。24/24 留有记录，22 次 Planner、12 次回退，p50 4.95 秒、p95 5.011 秒；短 Prompt 导致格式稳定性下降，已恢复原先包含字段约束的 Prompt。虚构最小 Schema 在同一候选 Adapter 中仍约 0.35～0.43 秒，说明较慢主要与真实 Planner 输入/输出复杂度相关；仅有一次运行结束后的 GPU 利用率快照，无法据此断言排队原因。R2 Gate 仍失败，未进行 Ownership-16、Claims-16 或完整 96。
- R3 本地实现保留完整原问 ROOT，并逐 Unit 运行本地通道、对 ROOT 与 Atom 文本作一次去重批量 Embedding、先 Unit 内 RRF 再 Root/Atom 两级有界融合，最后仅一次统一 Rerank。合成门禁覆盖错误 Atom 下的 Root 候选保留、每 Atom 种子配额、重复命中不随 Atom 数线性加分、ROOT+3 Atom 同 slot 一次 Batch，以及一次 Rerank；相关定向测试共 `88 passed`（两组各 44），Ruff 和 `git diff --check` 通过。R3 尚无真实候选验收；当前逐 Atom Grounding 仍只接纳 Atom provenance，ROOT 找到的正确证据可能被后续证据归属丢掉，必须在 R4 用结构归属接通后再验证回答。R2 Planner Gate 仍失败，阶段保持 PAUSED。
- R4 本地改造把 Root/Atom 候选与 canonical EvidenceGroup 按目标、组类型、行标签、列表导语和结构身份对齐；无锚点不再退回整批证据。共享章节标题不能单独给相邻兄弟组强锚点；短目标共享两个尾字也不能构成强锚点。纠错改为每个缺项选一个已对齐组，只读取该组成员的 previous/next Chunk，跨文档、版本、章节、结构组和表格行的候选不进入证据；全请求仍最多 12 Chunk、4 新组。相邻类别、职责列表、表格行、流程、无精确子串、无锚点及 Root 补救均有虚构合成测试。相关定向门禁 `53 passed`，Ruff 和 `git diff --check` 通过。仍须用 Ownership-16 人工核查真实组归属；当前组内的语义关系只作为诊断信号，不能凭这一轮合成测试宣称真实串答已解决。R2 Gate 仍未通过，阶段保持 PAUSED。
- R5 将模型输出缩为 `claims[{atom_id,text,support_ids}]`，服务端在逐条通过本地校验后分配 Claim ID、回填引用原文并计算 Coverage；一个 Claim 只对应一个 Atom，拒绝一条无依据 Claim 不会删除其他已通过 Claim。结构化输出使用 R1 已选的唯一 Schema 协议；JSON 格式错误不触发第二次完整生成。Repair 只为强证据却无已接受 Claim 的 Atom 发送小证据包，失败后保留已通过 Claim 的 LIMITED 答案。相邻的自然回答、Provider 和归属/纠错定向测试 `61 passed`，Ruff 与 `git diff --check` 通过；真实 Claims-16 尚未运行，不能声称校验误拒或串答已解决。
- 扩展检查中的 `test_product_api_real_loopback_grounded_qa_and_trace` 返回 `PROVIDER_UNAVAILABLE`，在只读归档的 R5 前提交 `7dddf6a` 上同样复现，属于当前环境/既有集成失败，未为通过门禁而改断言或关闭测试。`test_invalid_later_claim_is_buffered_and_never_publishes_a_prefix` 的既有断言预期两次 Provider Call，但同一基线实际只有一次；R5 将断言修正为不允许事实漂移触发第二次完整生成，未改该路径的生产行为。
