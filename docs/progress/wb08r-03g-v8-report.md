# WB08R-03G V8 修复与验收报告

本轮结论：**FAILED**，实现已冻结，`merge_allowed=false`，不具备独立 Verification 条件。真实 Preflight-16 仅 3 次事实/来源合格、13 次失败；最终离线代码还有 3 条既有正例回归。新增最终关系校验已用完用户允许的两轮实质修复，未开始第三轮。8289 已恢复原候选；8288/18288 生产未改动。未自动合并，未进入 04～06。

## 版本与部署边界

| 对象 | 实际版本/状态 |
| --- | --- |
| 分支、审计起点 | `codex/wb-08r-adaptive-rag`；`a06235053f692f4947d762f23c04b6855acc128a` |
| 首次功能代码、评测工具 | `5a4400d`、`a654323` |
| 最终运行代码提交 | `6a8ea584603e388f3512d6dc6e0bf8950fb7fc86`；未构建/部署 |
| 最终代码与评测工具提交 | `152bf163a97fbfac555269343dd9f798495e59b8`；报告所在提交另行交付 |
| 唯一质量候选 | `a654323adfe3c30b2bbda89704b4698e58482401` |
| 质量镜像 | `rag-test-wanshitong:wb08r03g-v8-a654323`；`sha256:1ee2dbbecfb3aad1031d8e865535168fd46208b4569f02f454a2a2f6bef41052` |
| 当前8289 | 已恢复 `bd00c56f15aa86cfbe875b1700c72b7a23d9d5da`；`rag-test-wanshitong:wb08r03f-bd00c56`；image `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c` |
| 生产 | 容器 `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`；image `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`，全程未替换 |

生产访问是54的18288转发至60的8288，未将18288误当60回环。最终只读核对生产容器ID、镜像、Config、HostConfig、挂载及8288映射与初始完全一致，healthy、HTTP200。Docker `Mounts` 返回顺序曾变化；首次回滚守卫因此在执行替换前终止，核对所有挂载内容后使用排序无关比较，确认不是配置变化。

候选替换 **2/3**：一次质量候选、一次恢复原候选；构建 **1次**，31.0817秒。失败镜像和旧镜像保留，未删除归档或卷。SSH桥接已关闭；受控诊断快照和回放证据保留。第二质量候选未构建，原因是已知功能回归和用户两轮上限，不是继续消耗剩余替换名额。

## W0 基线、构建和版本绑定

初始工作区仅10个原有未跟踪评测结果，最终逐文件摘要相同，未纳入提交。P0报告和manifest摘要保持原值，其 `PASS_WITH_DECLARED_PACKAGE_INPUT_GAPS` 是历史观测结论，不计入V8功能通过。

质量镜像使用锁定 `bd00c56` image `621acc79…` 完整替换 `rag_app` 包，386运行文件全部校对；37产品资产自检通过。先证明 Dockerfile、依赖锁、前端、迁移及公共资产相对该base无变化，再无网络构建、零依赖安装。未只覆盖单文件，未把 evaluation、私有正文或秘密放入构建上下文。运行树摘要 `55b2b274471a126d1525fa64baaeb921cf3fdf4bf997c90b8264f12f4b04e449`，资产manifest摘要 `e782386d75cf00cbfc9954c11452cdfa22d18514fe265c0a33b9d7416a497828`。

依赖锁SHA256为 `0e3548e867166c2d3374b03d9dc4930c7dfeb160c71ae43f35586adb4d69e619`；Python基础镜像digest为 `sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534`。全部配置摘要、资产和构建记录在本轮manifest。

活动索引 `irev_0329a35ca9ea700133f7b309160118f0`，46份资料/2880块，fingerprint `sha256:37ef53011feccfaf0bb01624c0127f026e028e64af0f89517eaa5e36dabc8714`，未导入、切换或重建。Provider仍为Qwen3-Embedding-0.6B/1024、Qwen3-Reranker-0.6B/TEI、Qwen/Qwen3-8B-AWQ，response_format、thinking disabled。rerank容量24、最大并发4未增加。

质量候选 generation 输入6144、输出预留1536、prompt `grounded-chat-v9`；planner transport timeout8秒、hard ceiling7000ms、观察线5000ms。实际serving fingerprint为 `sha256:b486d1cde3c7326f2f016ff898468b596c81e313ed2180f63f5a21e805bc9fae`，不是数据库旧值。启动核对健康、资产、索引数量、Provider认证GET/models200及模型存在；权限检查不冒充质量验收。

每次真实质量请求绑定代码/运行树/镜像/base/lock/资产/配置/索引/serving/Prompt/schema/packet/数据集/runner/trace/attempt/cache状态，SAFE逐请求账本见manifest `preflight_round_1.trials`。质量数据集 `wanshitong-v2-20260917:wb08r03f-truth-v1`，SHA256 `80b99f518dd0e9b78fb496fcbcf84ae6510d72f0cc2e1d0c4cecc4823737627b`；原runner SHA256 `99d71fc8e516a836d4c5f299b121b8684882b47e6855a48dcaf1820b4b1ff357`。

质量候选claim schema v7、query-plan v8、source-key v1、packet/budget v1；最终未部署代码的generation-evidence为v8、packet/budget为v2。相关revision进入serving/cache身份。不同代码的成绩未拼接，最终代码没有真实验收成绩。

## W1 N033：结构、语义、完整性分别结论

统一来源相容纯函数检查文档/版本/part/story/node、真实表格坐标、整型offset（拒绝bool）及连续/一致重叠范围；同一逻辑表行不同单元格仍需关系证明，合法TABLE_INTERSECTION表头不因跨行被误杀。来源组验证实际成员，不能只靠复制的group_id或complete标志。

N033已知S1/S2仍是同node同cell、`[0,41)`及`[41,52)`、group为null。P0错误特征测试改为目标正例；未伪造group、未把UNSUPPORTED改为SUPPORTED。原P0草稿结构门从 `CLAIM_SOURCE_MISMATCH` 转为通过，随后真实 `CLAIM_RELATION_UNSUPPORTED` 仍拒绝。

| 层次 | 结果 |
| --- | --- |
| 来源结构相容 | 合法连续片段通过；跨行、表、文档版本、故事、角色等反例保留 |
| 事实语义 | 合法合成完整服务正例通过；原P0真实草稿仍关系不支持，不能声称真实语义已通过 |
| 请求职责完整性 | 未验证通过；完整cell不代表整份职责列表完整 |
| a654323真实小门 | N033 3/3拒答；正确S1/S2在应用包，Provider预算选择时被丢弃，实际其他来源的Claim被关系门拒绝 |
| 后续离线修正 | 稳定来源阅读单元受保护，未再次部署或真实重跑，因此最终职责质量仍FAILED |

同时修复完整句尾吞下句、闭引号/末尾空白/段末、重复quote位置消歧、跨chunk及恢复幂等；正常已验证自然Claim不再一律扩写。需要恢复时形成最终有界事实后重新完整核验，renderer不追加事实。accepted=0的允许类别可最多一次局部repair，保留已接受Claim；未知引用、ACL/版本和真实冲突不通过重试规避。首次生成<=1、额外局部repair<=1，共享request预算。

## W2 稳定身份和真正发送包

来源稳定key与每Atom证书分离，S1/S2只在一个attempt内展示。矩阵保护、去重/重排/补组、allowance、冲突双方、最终引用和SAFE观测采用同一来源注册表。公开REST及History未新增字段；QueryRequest、PublicChatRequest、QueryResponse、EvidenceItem公开Schema与审计基线相同，新增内部字段明确排除公开序列化。

PreparedPacket记录真实别名映射、逐Atom许可、最小阅读单元、裁剪原因、消息/HTTP body摘要、预算估算和usage。Fake HTTP覆盖同步、流式、repair和transport fallback；未发送支持不可被模型Claim引用，不同attempt不复用S别名。DIRECT/服务端摘录独立记录，不伪造SENT。

真实16次中11个SENT包身份和许可一致，模型发布key属于其实际SENT；3次F015服务端摘录为sent N/A；2次预算在发送前受控终止。原候选未完整记录后两次失败packet，保留这一观测缺口。最终离线新增 `PREPARATION_REJECTED`，无HTTP body/usage、0 HTTP/0 repair，不能被当作SENT。私有recorder继续标ADAPTER_INPUT，并补存内部priority_source_units，默认关闭；到配额上限不再中断正常回答。

F024固定原请求揭示重复表结构元数据挤占预算：8来源仅62字，消息估算5060>4882。复用短结构引用后3772<=4882，schema1134，总估4906，输入6144/输出1536均不变，8来源的正文和SourceSpan不变。只执行本地prepare，HTTP0，不宣称该控制题真实通过。最小包仍超预算时继续类型化拒绝。

11个真实包估计值高于实际prompt usage 3192～3746 tokens；保留误差记录，不将估算声称为精确token数，也未扩大输入预算。

## W3 N031 的首个断点和后续边界

修复unit评分贡献顶替logical channel的传递问题，将unit_rank_contributions、retrieval_origins及retention_reasons分开；重复variant不重复投票。pre_fused与传统路径统一有限结构/Root/Atom/channel/document/table保留名额，先分配后截断。保护数量有界、顺序确定，未发送reranker的候选不伪造分数。

仅执行一轮真实retrieval-only诊断，固定快照SHA256 `4d4df05b4ae2a7f1012a22d901fb077970f27c2ad04d301483541d23e66b0797`；Planner0、Embedding1、Rerank1、Generation0、cache miss。Gold仅由外部评测器比较SAFE候选集合。

| 目标支持 | 历史路径首个可证断点 | a654323实际结果 | 最终离线结果及限制 |
| --- | --- | --- | --- |
| G01行名锚点 | lexical第11→fusion/hydrate第26；pre-rerank因DIVERSITY_POOL_CAP丢失 | 3/3请求进rerank input第7、output第20；随后应用包有它，Provider预算裁掉 | 稳定优先单元保留；无真实答案重跑 |
| G02输入事实 | raw各通道未返回，不能说它在fusion丢失；已有合法行锚点可闭合 | 正常62项闭合池中存在；因无rerank_rank被应用包跳过 | 合法闭合来源可入阅读包，不伪造rerank分数 |
| G03启动条件 | 同G02：raw未返回，合法行闭合可达 | 同G02，在应用物化阶段丢失 | 同上，三处冻结支持均进入重建包/优先单元 |

固定材料回放由1/3变3/3支持进入应用包，44项→Prepared13项，A1最小阅读单元6/6保留，估计5868<6144，零Provider。重建旧函数包39项而原live37项，多2项；这是固定材料合同回放，**不是精确HTTP重放**。

最新核对：实际62项闭合池含目标行6片段，**不含目标表头**；较早无rerank的82项诊断池有表头。故只证明值链闭合，不能证明完整表格关系上下文。表头首次丢失stage仍为NOT_OBSERVED；未以缺字段推断0，未继续扩修。真实N031仍3/3答案不足且来源不符，不能仅因ANSWERABLE或标题命中判通过。

## W4 通用性与未触发项

已实施evidence-first阅读准入：ACL、活动版本、可引用、来源及真实结构冲突仍硬过滤，旧UNSUPPORTED/MISSING/null group不一概剥夺阅读资格；阅读许可不等于事实证明。目标关系仍须最终核验。N031已出现合法原始锚点，因此未新增LLM hints、第二次Planner或第二次全量rerank，未加题号、文件名或组织别名词典。

冻结16个合成泛化样例，覆盖正式/口语/简称/错字、角色/阶段变化、跨chunk、表格交点/跨行、其他主题和无证据/诱导。构建前首跑15通过、G09失败；通用“组”主体识别修复后16通过，题目不改。只有15例可称未调规则首过；G09转为开发反例。该记录不冒充最终代码或真实Full96通过。

## 新反例与两轮停止账本

| 根因 | 实际修复次数 | 修复前→后 | 结束状态 |
| --- | --- | --- | --- |
| 来源分区恢复发布与问题无关的逐字事实（A001） | 2/2 | 新3失败/1通过→4通过；既有精准范围第1轮105通过/6失败，第2轮111通过/3失败 | 冻结FAILED，不进行第三轮，不降低断言 |
| 数值校验先删除阶段正文（N035） | 1/2 | 1失败/2负例通过→来源合同合计40通过 | 数字门修复；真实固定草稿仍缺口语关系证明 |
| N031合法closure未入包、来源优先单元丢失 | 原W2/W3范围内1轮 | 新4失败→通过，相关38通过 | 仅离线受益，缺表头/最终语义未通过 |
| F024重复元数据导致最小包超限 | 原W2范围内1轮 | 新4失败→4通过，相关31通过 | 固定prepare通过，未真实复验 |

A001原真实trace为 `trace_ceee1e20f61c4e17ed2e2d8590dd479b`：raw `CLAIM_SOURCE_MISMATCH`，恢复 `SOURCE_PARTITIONS_VALIDATED` 后错误发布5条与问题无关的原句。新校验限制最终问题→来源关系，不再让物理group完整替代请求完整。两轮后以下3条既有正例仍拒绝，accepted/published/repair均0，保留反例：

- `tests/application/answering/test_grounded_claim_v5_quotes.py::test_pack_preserves_validated_paraphrase_without_source_rewrite`：`CLAIM_QUERY_RELATION_UNSUPPORTED`。
- `tests/application/answering/test_grounded_claim_v5_quotes.py::test_pack_expands_selected_fragment_to_full_source_sentence`：保留初始 `CLAIM_FRAGMENT_INCOMPLETE`，恢复后关系门拒绝。
- `tests/application/answering/test_grounded_claim_v5_quotes.py::test_pack_revalidates_full_source_when_model_rewrites_the_fact`：保留初始 `CLAIM_TEXT_UNSUPPORTED`，恢复后关系门拒绝。

未删除负例、未把这些失败改skip/xfail，也未撤掉安全门来让测试变绿。最终代码因此不具备新候选部署条件。

## W5 实际验证

不同阶段测试存在重叠，不相加，也不使用P0的220/5作为本轮成绩。

| 范围 | 实际执行结果 | 证据限制 |
| --- | --- | --- |
| 构建前回答链/Provider发送包/检索 | 347通过；366通过；956通过，最后检索局部57通过 | 各自功能范围，有重叠 |
| 构建前最终相关组合 | 1702通过/3失败，247.12秒 | 新Fake错误修正后单项1通过；另2项同名基线失败，未声称整体全绿 |
| 最终两轮关系+阶段精准范围 | **111通过/3失败** | 阻断新质量候选 |
| 阶段/数字与来源合同 | 40通过 | 与上一行重叠；真实口语最终关系仍失败 |
| 最终W2预算/HTTP/alias | 31通过 | 包括新4个反例前失败后通过 |
| 最终W3阅读物化/身份 | 38通过 | 包括新4失败修复及额外结构边界 |
| 最终完整服务attempt合同 | 9通过 | 预算拒绝有SAFE packet，HTTP0/repair0 |
| 最终runner/指标 | 20通过 | 包括无Gold题型运行路径及预算拒绝不伪造sent |
| 私有recorder内部单元 | 前1失败，补精确字段后5通过 | 公开序列化仍排除该字段 |
| 静态与语法 | 构建前mypy466源文件通过；最终修改模块mypy通过；17个最终Python文件Ruff/format/compile、diff检查通过 | 不掩盖功能失败 |

构建前两个基线旧harness失败为 `test_runtime_tables.py::test_empty_and_omitted_table_markers_are_reproducible` 和 `test_v3_02_baseline.py::test_concrete_runner_writes_52_case_and_trace_receipts`。另外一项新增Fake把repair当首次请求读取question，已修测试Fake并复跑通过，没有调整运行逻辑。基线更广快照曾报3576通过/144失败/89 deselected，隔离archive环境有局限，不能把144项都认作旧代码问题；按用户要求停止扩大历史门禁，精确终止泄漏的本次后台测试进程。

构建前universal-first/硬编码扫描通过，公开Schema摘要相同。全库既有Ruff23项、文档检查418个不同缺项如实保留，不扩展为无关清理。最终仅做受影响的功能检查、必要语法/类型/保护文件核对；未追加全库长套件。

精准命令及文件：

```text
.venv/bin/python -m pytest tests/adapters/providers/test_generation_budget_units.py tests/adapters/providers/test_prepared_generation_packet.py <3个已记录的自然包预算测试> -q
.venv/bin/python -m pytest tests/application/retrieval/test_generation_reading_units.py tests/application/retrieval/test_generation_evidence_pack.py tests/application/retrieval/test_atom_evidence_identity.py tests/application/retrieval/test_atom_support_binding.py -q
.venv/bin/python -m pytest tests/application/answering/test_wb08r03g_attempt_contract.py -q
.venv/bin/python -m pytest tests/evaluation/test_wb08r03g_v8_runner.py tests/evaluation/test_wb08r03g_v8_metrics.py -q
.venv/bin/python -m pytest tests/product/test_private_replay_capture.py -q
```

W2三项完整nodeid是 `test_natural_grounded_answer.py::test_natural_prompt_prunes_complete_groups_within_budget`、`::test_natural_prompt_protects_named_table_row_when_budget_is_tight`、`::test_natural_prompt_protects_direct_support_without_group`。SAFE验证收据含精确静态命令；更早范围日志位于 `/tmp/wb08r03g-v8-tools`。

## 同一候选真实Gate

全部16次使用独立会话、相同a654323版本绑定、0 cache hit、16/16唯一final，保留全部运行，不挑最好结果。私有捕获环境关闭。另2次F024/N035受控进程内服务捕获只用于固定输入，未混入质量成绩、未改变HTTP候选配置。

| Preflight子集 | 运行数 | 事实/来源通过 | 失败 |
| --- | ---: | ---: | --- |
| N031 | 3 | 0 | 锚点已入rerank，但最终支持丢失，答案不完整/来源不符 |
| N033 | 3 | 0 | 正确连续支持在发送前丢失，拒答 |
| F015 | 3 | 3 | 冻结三处事实的版本/node/offset/逐字摘要匹配；系统Atom覆盖仍PARTIAL，保留差异 |
| F013 | 3 | 0 | 正式对照拒答 |
| F024、F034 | 各1 | 0 | INPUT_BUDGET_BLOCKED，不算预期安全拒答通过 |
| N035 | 1 | 0 | 数字关系误拒；修正后仍有口语关系边界 |
| A001 | 1 | 0 | 无关来源恢复后被错误发布，严重语义失败 |

| Gate | 实际结果 | 原因 |
| --- | --- | --- |
| Preflight-16 | **FAILED，3/16** | 13次失败 |
| A EvidencePack-12 | NOT_RUN | 小门失败 |
| B CoreAnswer-16 | NOT_RUN | 小门失败 |
| C Failed-24 | NOT_RUN | 小门失败 |
| D Natural-36 | NOT_RUN | 小门失败 |
| E Full-96 | NOT_RUN | 小门失败，不浪费全量调用 |
| F Concurrency-4 | NOT_RUN | 小门失败，未声称4并发验收通过 |

旧pre-pack指标仍保留，但旧汇总的分母只有3条自动Gold题，不能把其1.0当16题全通过。逐答案摘要绑定的私有事实审核结果是3通过/13失败；安全收据和SAFE逐请求manifest共同佐证。后续性能按同16条trace重新分类，明确为post-processing，未修改原runner身份或混入新请求。

## 性能和调用账本

观察线在运行前固定：首阶段p95<=1秒、Direct/Refuse<=8秒、Generate<=15秒、Planner<=5秒；不以失败或拒答速度代表正常成功问答。

| 路径 | n | 端到端p50 / p95（ms） | 首阶段p95（ms） |
| --- | ---: | --- | ---: |
| 服务端Direct摘录 | 3 | 5509.95 / 5624.21 | 47.00 |
| 有模型答案生成 | 4 | 12540.27 / 13095.92 | 74.84 |
| 拒答 | 7 | 8491.26 / 8783.55 | 53.42 |
| 发送前预算阻断 | 2 | 2544.165 / 3826.87 | 46.02 |
| 结构生成、repair | 各0 | NOT_OBSERVED | NOT_OBSERVED |
| Planner实际调用 | 9 | 2177 / 3624 | N/A |

四次生成答案本身质量失败，此延迟不构成成功生成性能PASS。Refuse p95超过8秒观察线，单列 `PERF_FAILED`，不是DEMO_READY。首阶段为客户端首个stage-status事件；模型TTFT和排队等待没有独立完整观测，均NOT_OBSERVED。Direct首答案p95为5621.45ms，生成首答案p95为13093.03ms，不能冒充模型TTFT。

正式16次总调用：Planner9、Embedding16、Rerank16、Generation11、rewriter0、repair0、transport retry0，每请求最多各1次，未隐藏流式降级调用。额外retrieval-only为Embedding1/Rerank1；两条私有服务捕获合计Planner1/Embedding2/Rerank2/Generation1。全轮已记录调用合计Planner10、Embedding19、Rerank19、Generation12，未把诊断算质量成绩。4并发、History/Trace跨会话真实并发验收未运行，不以顺序16个唯一final代替。

## W6 交付与剩余项

只提交本轮源码、合成回归、评测工具及三个安全报告/状态文件；原10个未跟踪评测结果保留。提交按功能意图组织，普通推送当前分支；最终报告提交SHA和远端引用核对保存到受控目录 `git-delivery.safe.json`。本报告所在提交不改变最终运行代码，不作为新质量候选。

需保留的未解决项：最终关系校验3个既有口语/恢复正例；N031表头闭合与最终所问关系完整性；N033/F013完整职责真实输出；N035口语关系证明；F034及其他控制题真实复验；Refuse性能与全部A～F同版本验收。最终W2/W3离线修正没有真实部署，不能认作这些题通过。

私有材料保留在 `C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03g-v8-20260919`；P0仍在相邻 `wb08r03g-p0-20260919`。远程受控诊断目录 `/tmp/wb08r03g-v8-diag-1ip6ra2o` 保留必要快照/回放。最终新增src/test差异及三个报告对45条私有逐字片段检查为0匹配；保护文件摘要、JSON及16条trace身份核对通过。正文、秘密不进入Git或镜像。关键SAFE文件摘要见manifest `safe_evidence_receipts`，包括N031路径/包回放、N033/关系反例、预算回放、Preflight审核、性能及最终生产/回滚身份。

用户两轮上限已经触发，本轮以FAILED结束。没有等待下一份P1/P2提示，没有继续无限补丁；`completed_phases`仍只有00、01、02。
