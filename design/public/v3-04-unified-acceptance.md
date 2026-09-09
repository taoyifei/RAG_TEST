# V3-04 统一非回归、能力继承与发布验收

日期：2026-09-09

公开性：本文只记录公开合成语料、匿名聚合、代码/镜像身份和门禁结果。私有原件、
文件名、正文、完整问题/答案、逐文件 input/附件 hash、原图、凭据和 FULL Trace
只保存在用户授权的本地 0600 证据中，不进入 Git、构建上下文或 Review ZIP。

V3-00.6 至 V3-03 的必修源码闭环已复核；V3-04 又修复了同一事实跨问法、混合
标识符、来源限定及跨 chunk 表格闭合的证据选择退化。公开冻结回归和本地私有开发
回放均通过，Parser/Chunker 自 V3-00.6 后的有效身份保持不变。

该结论是“这些公开结构、私有匿名问法组和安全边界已修复”，不是“所有 Word 或
自然语言均已覆盖”。真实 LLM/OCR/视觉质量、正式 release acceptance 和生产发布
没有完成。最终候选仍缺与精确镜像绑定的有效人工风险处置，因此发布安全门为
`BLOCKED`，上游失败后正式 acceptance 为 `NOT_RUN`；没有 push、merge 或部署。

## 最终状态

| 状态项 | 状态 | 依据与边界 |
| --- | --- | --- |
| `DOCX_VALIDATION_ROOT_CAUSE_AND_FIX` | `PASS` | 纯空白可引用 atom、separator-only 表格和派生编号边界在最早偏差处局部修复 |
| `DOCX_GENERAL_CONTRACT_MATRIX` | `PASS` | 标题、列表、表格、文字、边界、其它结构、确定性/回读及 fail-closed 矩阵 |
| `INGESTION_TRACE_AND_SAFE_ERRORS` | `PASS` | 根路径 `$`、有界脱敏字段、cause、阶段身份及唯一 success/failed/cancelled 终态 |
| `PRODUCT_INGESTION_E2E` | `PASS` | HTTP 上传、Job、完整校验、原子激活、查询、原件回读与冷启动 |
| `SCREENSHOT_INCIDENT_REPLAY` | `PASS` | V3-00.6 已匹配原件并在默认 Product 重放；V3-04 未把其它未匹配截图误计为复现 |
| `CURRENT_VALID_CHUNKING_PRESERVED` | `PASS` | V3-04 Parser/Chunker fingerprint 与 B 相同；69b064f 后相关源码无变化 |
| `QUERY_UNDERSTANDING_PARAPHRASE_CONTRACT` | `PASS` | 原问保留、共享类型化语义、有界改写、Hybrid 贯穿、来源限定与同义组一致 |
| `LLM_CONFIGURATION_AND_ACTUAL_CALL_VISIBILITY` | `PASS` | 配置不等于调用；冷/缓存/未配置/未授权/预算/失败路径显示本次实际调用事实 |
| `GROUNDED_PARAPHRASE_PRODUCT_E2E` | `PASS` | 公开 Product 与私有本地 8 个可答问法检查均有答案、Evidence 和引用；2 个反例拒答 |
| `QUERY_HARD_CONSTRAINT_SAFETY` | `PASS` | 数字、单位、否定、时序、版本、标识符、范围和引用原词不可由改写越过 |
| `DOC_TEXT_MEDIA_ADAPTER` | `PASS` | 受限 DOC→清洗 DOCX→当前 IR/Artifact/Chunk 链，原始身份与派生身份分离 |
| `DOC_PRODUCT_MEDIA_E2E` | `PASS` | 公开真实 DOC 上传、媒体预览、冷启动及私有 10 份 DOC 匿名回放 |
| `DOC_CONVERTER_ISOLATION` | `PASS` | 非 root、Landlock、seccomp、rlimit、进程组终止、隔离目录和禁网探针 |
| `OCR_CONTRACT` | `PASS` | local/remote adapter 共用有来源合同、限额、缓存身份和新 Revision 发布 |
| `OCR_LIVE_QUALITY` | `BLOCKED` | 未获真实模型数据出网与累计预算授权；真实识别分母为 0 |
| `DIAGRAM_RELATION_CONTRACT` | `PASS` | candidate、方向/歧义、review、occurrence、来源类型及新 Revision 合同 |
| `DIAGRAM_RELATION_LIVE_QUALITY` | `BLOCKED` | 未调用真实视觉模型；合成消费者测试不证明关系语义质量 |
| `TRUE_STREAM_TCP` | `PASS` | Provider 未结束时真实 loopback TCP 已交付响应头、首字节和已核验 claim |
| `TRUE_STREAM_BROWSER` | `PASS` | 桌面/移动 Chromium 的增量显示、唯一 final、停止和 KB 切换隔离 |
| `CANCELLATION_BUDGET_SCOPE_SAFETY` | `PASS` | 断连占槽、实际 usage、无成功缓存、重试、来源删除及 Session/Token 撤销 |
| `SELECTED_ALGORITHM_NON_REGRESSION` | `PASS` | 同一冻结 `fts5-only` 配置 C 的 16 个 accepted gates 全通过，无安全计数退化 |
| `ALGORITHM_IMPROVEMENT_EVIDENCE` | `PARTIAL` | 同配置 B→C 有明确离线改进；holdout 已参与修复诊断且无真实 Dense/LLM/容量分母 |
| `FULL_LOCAL_GATES` | `PASS` | 最终候选运行完整离线 Python、marker 核对、前端、OpenAPI、build 与真实浏览器门禁 |
| `BUILD_CONTEXT_GATE` | `PASS` | 最终 clean 提交使用既有最小 immutable context 构建；外部收据绑定 source/image/manifest |
| `RELEASE_SECURITY_GATE` | `BLOCKED` | 精确候选没有匹配、有效且获批的完整 OS 风险处置；模型未代签 |
| `RELEASE_ACCEPTANCE` | `NOT_RUN` | 安全门阻断后按依赖失败即停，不运行正式候选 acceptance |
| `MERGE_READINESS` | `PARTIAL` | 本地源码/测试可供评审；发布批准、真实多模态/LLM质量、push 与合并均未完成 |

`PASS` 只覆盖各行明确的合同与执行分母；两个 Live 质量 `BLOCKED` 不填 0 或 100%。
`MERGE_READINESS=PARTIAL` 也不等于授权合并：当前分支只形成本地提交，必须先由人
审阅安全收据及 Review 包，再决定是否 push/merge。

## 冻结身份与证据分层

- 分支：`codex/universal-selective-word-v3`。
- main 只读远端：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`。
- Industry 只读远端：`5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。
- 两者 merge-base：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`。
- 远端 Universal 仍为 `0505fac076ee468916f988b35345c71027049678`；本轮未 push。
- V3-04 修复后行为提交：`7c41883aa84dca57fccc88b294d95bf267c5fd30`。
- 后续 `194504d` 仅对齐公开 Trace 脱敏夹具，`03c36ae` 仅隔离本地浏览器视口
  测试服务；均未改变 Parser/Chunker 或查询选择语义。
- 最终提交、Docker image ID、OCI revision、平台和 context manifest digest 由
  `artifacts/v3-04/release/` 中的机器可读收据在提交后读取，避免在 Git 文件中
  伪造自引用 SHA。

证据分为五层，互不替代：

| 层 | 本轮证据 | 能证明 | 不能证明 |
| --- | --- | --- | --- |
| 源码/单元 | 静态检查、mypy、pytest | 合同、不变量、失败关闭 | 真实服务质量或发布身份 |
| loopback Product | HTTP、真实 TCP、Chromium | 默认 Product 可达、协议和页面行为 | 外部模型质量、生产代理行为 |
| 私有本地回放 | 禁网容器、匿名聚合 | 指定私有结构/问法已回归 | 独立 holdout 或开放域泛化 |
| 公开冻结 Evaluation | 同配置 B/C、52 cases | 已声明切片的离线非回归 | 未见 holdout、真实 Dense/LLM 或生产容量 |
| Build/Release | 最小 context、镜像、扫描 | 精确候选工程身份和发布阻塞 | 被阻塞时的正式 release acceptance |

## V3-00.6 至 V3-03 复核

| 阶段 | 根因 | 已完成正向/负向保护 | 剩余边界 |
| --- | --- | --- | --- |
| V3-00.6 DOCX | 合法纯空白 OOXML 被当作可引用 atom，继而触发根级 `Chunk` ValidationError；诊断丢失 `$` 路径 | 正常 golden、空白段/表格、重复稳定 ID、列表编号边界、失败/取消/成功终态、默认 Product 和截图事故重放 | 非穷举 Word 生产器与生产容器未声明 |
| V3-00.7 语义问答 | 多套正则语义不一致、Evidence 不足后不改写、改写未贯穿 Hybrid、原问/改写可刷票、调用事实混淆 | 同义组、列表/表格/职责/步骤、硬约束、无来源比较拒答、冷/热缓存和 Product 浏览器 | 真实 LLM 质量仍 `BLOCKED` |
| V3-01 DOC 图文 | legacy DOC 文本降级丢表格/媒体且转换边界不完整 | 真实 OLE DOC、受限转换、Artifact/MediaScan、重复图片 occurrence、冷启动与沙箱访问探针 | 媒体总量可为 partial/unknown；不等于 OCR |
| V3-02 OCR/关系/Trace | Product 缺统一来源合同和分层 Trace；Provider child elapsed 曾被父 span 裁剪 | OCR adapter 消费者、关系 candidate/review/new revision、Trace 层级/隐私/导出/恢复及固定 Chunk 基线 | 真实 OCR/视觉质量 `BLOCKED` |
| V3-03 流式 | `stream=true` 在同步回答结束后才包装 SSE；浏览器也先缓冲完整 body | TCP 提前交付、完整 claim 校验、背压、三类 timeout、断连/撤权/删除、桌面/移动停止隔离 | 真实 Provider TTFC/费用/取消浪费 `BLOCKED` |

阶段报告分别位于 `docs/progress/v3-00-6-docx-ingestion-contract.md`、
`docs/progress/v3-00-7-semantic-query-contract.md`、
`design/public/v3-01-doc-media-adaptation.md`、
`design/public/v3-02-operational-trace-multimodal-baseline.md` 和
`design/public/v3-03-true-streaming.md`。

## V3-04 新复现、根因与局部修复

进入 V3-04 的同配置 B 不是“检索不到”：20 个可答 holdout 的 `Recall@5=1.0`，
但只有 12 个进入有效 Evidence/引用，最终 answerable accuracy 为 0.6667。最早
退化位于查询解释、结构扩展和 Evidence 发布之间：

1. 中英混合且带分隔符的标识符没有完整保留，开放式标识符问法可命中候选却不
   能发布精确来源。
2. 对表格坐标的规则性判断过粗，合并/偏移表格的真实 node 映射没有被逐格核对，
   有效 SourceSpan 被整体拒绝。
3. “谁负责/归谁管/职责是什么”等问法对同一关系的支持目标不一致；Evidence
   可能只保留主体或一段值，没有闭合完整关系和多个职责片段。
4. 跨文档同名角色需要动态来源限定，但限定可能在改写时丢失；候选窗口又可能先
   被其它文档占满，使正确表格行未在预算内闭合。

修复仅修改 Analyzer、共享 semantics、answer support、Evidence 选择、Neighbor
扩展和 serving/query cache 身份：

- `69b064f` 保留混合标识符，按可信 node/逻辑坐标验证表格，统一开放标识符和
  关系问法的证据发布。
- `8b1dad7` 从通用自然语言中提取动态来源限定，改写后仍保留；多个来源同时匹配
  时继续拒答，不用文件名白名单。
- `6c50c2f` 曾尝试用标题层级选择所谓“更一般”的职责表；公开/私有反例证明该
  规则会把未知结构猜成层级语义，因此没有作为最终策略保留。
- `7c41883` 改为只在来源限定唯一指向一个 document version 时，优先让其种子使用
  有界表格闭合预算；不改变直接候选排序，不唯一时保持原顺序和歧义拒答。

没有加入业务文件名、组织名、私有问题、答案或 hash；没有全局调大 top-k、Evidence
预算、chunk size 或阈值。`docx-ooxml-v4@4.0.0` 和
`docx-structural-v3@3.0.2` 的 fingerprint 在 B/C 中完全一致。

## 公开冻结评测与 A/B/C

公开合成数据集为 `p08-synthetic-v2`，SHA-256 为
`915fa71ead95617bb5f7cb7d09660366212934f3949da1773c180b4aa8654dd7`；共 52
cases，28 个 tuning、24 个选定回归（20 可答、4 不可答）。B/C 均使用：

- seed `20260903`、`p08-offline` / `dev-offline`；
- strict `docx-safe-v1`；target/hard max/overlap/min tail 为
  `384/512/64/64`；
- 同一 `fts5-only` selected candidate、相同 policy 和 fixture catalog；
- Parser/Chunker 同版本同 fingerprint；
- 无外部服务、Provider、Embedding 或 Reranker 调用；
- 每次 run manifest 的 `holdout_access_count=1`。

| 指标 | A main/Industry | B 修复前 Universal `a760637` | C 修复后 Universal `7c41883` |
| --- | ---: | ---: | ---: |
| 可比性 | `NOT_COMPARABLE` | 同配置 | 同配置 |
| answerable accuracy（24 总样本） | 不填分数 | 0.6667 | 1.0 |
| refusal precision / recall / F1 | 不填分数 | 0.3333 / 1.0 / 0.5 | 1.0 / 1.0 / 1.0 |
| Recall@5（20 可答） | 不填分数 | 1.0 | 1.0 |
| Evidence document/chunk precision | 不填分数 | 0.6 / 0.6 | 1.0 / 1.0 |
| Citation document/chunk/validity | 不填分数 | 0.6 / 0.6 / 0.6 | 1.0 / 1.0 / 1.0 |
| Source range recall/precision/coverage/F1 | 不填分数 | 0.6 / 1.0 / 0.6 / 0.75 | 1.0 / 1.0 / 1.0 / 1.0 |
| irrelevant Evidence / unsupported claims | 不填分数 | 0 / 0 | 0 / 0 |
| wrong scope/revision/vector attempts | 不填分数 | 0 / 0 / 0 | 0 / 0 / 0 |
| P50 / P95 本机同步时延 | 不填分数 | 7.43 / 13.86 ms | 7.53 / 12.67 ms |
| 16 个 accepted gates | 不运行 | `FAIL` | `PASS` |

A 没有在相同语料、模型、配置、硬件、预算、缓存和默认入口上运行；旧源码存在的
能力不能换算成 0 或 100%，也没有为本轮强制恢复旧生产环境。C 的检索 Recall@5
没有变化，改进来自“命中后能否选择完整、正确、可引用的 Evidence”，而不是换成
另一个候选算法。

16 个阈值在 C 前沿用既有 accepted requirements：scope/revision/vector/unsupported/
overflow/irrelevant 必须为 0；citation validity、source coverage、answerable
accuracy、refusal F1 必须为 1；Recall@5 至少 0.9；citation document/chunk precision
至少 0.8；source precision/recall/F1 分别至少 0.75/0.9/0.8，并满足各自最小样本。

需要诚实限定：虽然最终 run 自身只打开一次 holdout，阶段中已经查看 B 和中间运行
的失败明细来修复通用缺陷。因此这 24 个样本是严格、冻结、可复现的回归集，但已
不再是从未见过的独立 holdout。由此 `SELECTED_ALGORITHM_NON_REGRESSION=PASS`，
而更广泛的 `ALGORITHM_IMPROVEMENT_EVIDENCE=PARTIAL`；不能把 1.0 外推成开放域
泛化或全面优于旧版。

## 私有开发回放与内容/结构分母

用户明确指定的两个本地目录只在 `--network none` 容器中只读使用。逐文件 input
hash、原始/派生/媒体附件 hash、相对路径和结果映射保存在本地 0600 manifest；
公开仓库和 Review 包只保留以下匿名聚合：

- 16/16 文件成功处理，其中 6 DOCX 的 native text/structure 为 PASS，10 DOC 因
  原格式媒体总量不可证明而为 PARTIAL；失败 0。
- 1714 chunks，稳定 ID 重复 0；可引用源文字 77,972 chars，唯一覆盖 77,972，
  minimum SourceSpan coverage 1.0，missing 0。
- 562 个表格行/1446 个单元格；表示 553 行/1376 单元格。未表示的 9 行/85
  单元格均无可见文字，不制造空白正文。
- 446 个列表标签，表示 445；唯一未表示标签无可见文字。
- 127 个独立 embedded media、133 个显示 occurrence；media inventory 为 6
  complete、1 partial、9 unknown，不把 unknown 写成 0。
- Artifact 聚合为 16 source、10 derived、127 embedded；外部服务调用 0。

同一私有集合中，6 个与开发问法相关的文档又经默认 Product 离线完成 6/6 Job、
1409 chunks、FTS 和必需 vector space 持久化。10 个冻结检查中，8 个可答、2 个
不可答：三个自然表达组分别 3/3、3/3、2/2 都有答案、Evidence、冻结术语覆盖且
主文档一致，2/2 反例拒答。真实 Provider 调用为 0。

这些私有组织/职责资料和已知问法被明确标为
`development-regression-not-holdout`，不参与 tuning/holdout 分数，也没有把内容
或答案搬进生产规则。V3-00.6 的已匹配截图事故独立保留；V3-04 不用目录中其它
文件去冒认任何未匹配截图。

## main / Industry / Universal 能力继承结论

持续维护的逐提交矩阵位于 `docs/migration/industry-port-matrix.md`。V3-04 复核的
用户感知结论如下：

| 能力 | main / Industry 参照 | 当前 Universal 默认 Product | 决策 |
| --- | --- | --- | --- |
| 默认入口 | legacy `/api/chat` 与旧静态页 | `/api/v1/...:answer` + React Product | `KEEP_CURRENT` |
| 原问与合法改写 | 旧 rewrite 保留原问、最多一个改写；Industry 有主体锚点 | 类型化语义、原问/硬约束权威、补救改写贯穿 Lexical/Dense/Evidence | `ADAPT` |
| Hybrid 与 Evidence | 旧 BM25/Dense/RRF 和来源支持有可借鉴行为 | 当前 Exact/FTS/Dense/RRF/Rerank、active revision 和 SourceSpan | `KEEP/REPAIR` |
| LLM 回答/回退 | 旧 generation 路径；默认入口、缓存与权限模型不同 | 配置/授权/预算/实际调用可见，claim/citation 校验后发布，合理摘录回退 | `ADAPT` |
| DOCX/DOC 图文 | 旧链没有当前 IR/Artifact/Revision 完整合同 | OOXML v4、结构 Chunk v3、受限 DOC 转换、媒体 occurrence | `KEEP/REPAIR` |
| Trace | Industry legacy Trace 较完整 | Product 双存储、统一 trace ID、权限/TTL/导出/备份 | `ADAPT` |
| 真流与停止 | Industry legacy NDJSON 有流式行为 | 版本化 SSE、完整 claim、背压、timeout、停止与在途重鉴权 | `ADAPT` |
| 工业部署/update/last-good | Industry 专用 | 未复制到通用默认路径 | `DEFER/REJECT_SPECIFIC` |

无关账号平台、GPU 模型中心、完整工业部署或旧 Runtime 不因“能力继承”而整体合入。
本轮明确要求的 DOCX、自然问法、Evidence、图文和流式没有降为 DEFER。

## 模型调用、缓存、流式和费用分片

- 离线答案可以是 `extractive`，但 UI/API 必须显示 generation/rewrite/rerank/query
  embedding 本次是否实际调用；缓存命中时本次调用数为 0。
- Provider 配置只证明 resolver 能绑定模型，不证明本次调用、质量或授权。真实
  LLM、OCR、视觉请求分母均为 0。
- 公开 Evaluation 是同步非流式本机进程，P50/P95 不能与 TCP/浏览器 TTFC 混用。
- TCP 流只证明 Provider 未结束前交付，2 秒是测试 deadline 而非测得 TTFC。
- 浏览器流式场景整段约 3.2s/3.9s，包含登录、建库、上传、索引和多请求，不是
  首内容时延。
- 断连合成 Provider 记录 1 次调用/30 实际夹具 tokens，整体状态 CANCELLED、无
  成功缓存；重试产生第 2 次调用。该分片不冒充远程费用。

## 版本、重建与重启影响

| 变化 | 身份/缓存 | 所需动作 |
| --- | --- | --- |
| V3-00.6 空白/边界修复 | `docx-structural-v3` 升至 3.0.2，index fingerprint 变化 | 使用该 Chunker 的 KB 构建新 revision、完整校验后原子激活 |
| V3-00.7 与 V3-04 查询/Evidence | retrieval implementation 最终为 `v3-04-semantic-query-v4`，serving/query cache identity 前进 | 重建应用/正常重启；旧回答缓存不复用；不重解析/重分块/重嵌入 |
| V3-01 DOC 转换 | `word-document-v1` 2.x 与沙箱 recipe 绑定 | 受影响 DOC 重新转换并构建新 revision；原生 DOCX 不因转换器重建 |
| V3-02 OCR/关系 | adapter/config/model/policy/revision 纳入缓存身份 | 仅实际启用 OCR 或发布关系时生成新 revision；默认关闭能力不伪激活 |
| V3-03 SSE/前端 | 协议和客户端代码变化，成功缓存语义不变 | 重建后端/前端并重启；无需索引重建 |
| V3-04 发布审计修复 | OpenAPI 构建链把 `js-yaml` 从 4.3.1 定向 override 到 4.3.2；生成 schema 不变 | 重建应用镜像；无需索引重建或缓存迁移 |

任何环境都不能给旧镜像换标签冒充新 revision。发布时必须核对本地代码 HEAD、
wheel/进程 revision、image ID/OCI revision、配置、active index 和 cache identity。

## 外部研究与后续一次一项消融

本阶段只把公开论文、开源实现和成熟知识库文档作为后续候选，没有据此默认替换
当前策略：

1. [RAGChecker](https://arxiv.org/abs/2408.08067) 及其
   [开源实现](https://github.com/amazon-science/RAGChecker) 提供 retrieval/generation
   细粒度诊断思路；可先离线映射到现有 Evidence/claim 指标，不引入在线 Judge。
2. [Qdrant RAG evaluation](https://github.com/qdrant/qdrant-rag-eval) 可用于补充
   可复现检索基准；必须保持当前主备 vector space、Revision 和权限隔离。
3. [RAGFlow parent-child chunking](https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/configure_child_chunking_strategy.md)
   可能改善长表/层级召回，但会改变结构和索引身份，须以单一变量消融证明非劣，
   不能直接替换当前结构 Chunker。
4. [Dify knowledge retrieval metadata filtering](https://github.com/langgenius/dify-docs/blob/main/en/cloud/use-dify/nodes/knowledge-retrieval.mdx)
   支持把来源限定提升为显式元数据过滤候选；后续应先设计可信 metadata 字段和
   歧义失败语义，再与当前动态限定比较。
5. [CReSt](https://arxiv.org/abs/2505.17503) 与
   [T2-RAGBench](https://arxiv.org/abs/2506.12071) 涉及更复杂的测试时适配/检索评估；
   工程量与泄漏风险更高，只进入研究 backlog。

优先 backlog 是建立执行者不可见的全新公开 holdout、真实非生产 Provider 分片和
多次重复运行的波动区间；之后每次只改变 parent-child、metadata filter、融合或
预算中的一个变量。任何候选必须继续满足 scope/revision/vector/引用/硬约束和
取消安全门，不能以平均质量提升掩盖错误来源或错误作答。

## 最终门禁、发布阻塞与 Review 包

完整开发门禁包括 `scripts/dev.py check`、marker collect-only、`doctor/smoke/
product-check/product-smoke`、前端 `api:check/lint/typecheck/test/build`、真实
desktop/mobile Chromium、`git diff --check` 和私有文件泄漏审计。最终命令、
passed/deselected/skipped、耗时和 commit SHA 以提交后生成的
`artifacts/v3-04/release/` 收据及最终交付为准。

首次对 `1a6165f` 候选执行 `release.py verify` 时，Python dependency audit 通过，
随后 npm audit 因 `openapi-typescript 7.13.0` 的
`@redocly/openapi-core 1.34.19` 精确依赖 `js-yaml 4.3.1` 而报告 2 个 High，并在
Secret/Trivy/SBOM 前失败即停。修复只增加父依赖范围内的 `js-yaml 4.3.2`
override 和对应 lock 节点；`npm ci`、依赖树、audit、完整前端及浏览器门禁须在
最终候选重新通过。该失败尝试不作为最终发布安全门通过证据。

应用候选镜像本身不包含完整离线部署资产。裸 `asset-selfcheck` 缺
`/app/deployment/ASSETS.sha256`，挂载当前 deployment 目录后又检测到新构建
frontend 与旧资产 manifest 不同；因此完整离线部署 bundle 自检不纳入
`BUILD_CONTEXT_GATE=PASS`，仍属于未运行成功的独立交付门禁，不能由 app image
构建或源码浏览器测试替代。

最终提交后沿用 V3-00.5 的 immutable minimal context 执行 `release.py build`，
并用同一风险判定执行 `release.py verify`。若精确 image ID 没有匹配且有效的人工
OS 风险处置，`RELEASE_SECURITY_GATE` 必须保持 `BLOCKED`；完整原始 Trivy、可修复
子门、SBOM、license、Secret scan 和风险审核身份分别保留。模型不能生成真实
`APPROVED` 记录，旧镜像审批不能迁移到新镜像。

安全门阻断后不执行 `release.py acceptance`，也不启动隔离正式候选、生产发布或
active alias 变更。源码/loopback 测试继续标为开发证据。

脱敏 Review ZIP 使用显式公开 allowlist、固定成员顺序、时间和权限，并包含成员
SHA manifest；以下内容必须排除：私有原件/派生件/图片、私有 manifest/hash、问题
与答案、FULL Trace、数据库、Secret、Trivy cache 和非公开本机路径。外层 ZIP
SHA-256 无法自包含，故由最终交付和包外 `.sha256` 收据给出；包内
`MANIFEST.sha256` 另行逐成员验证。

## 本地提交与结论边界

V3-04 行为提交：

- `69b064f` `fix(retrieval): 修复跨问法证据选择退化`
- `8b1dad7` `fix(retrieval): 保留职责问法的来源限定`
- `6c50c2f` `fix(retrieval): 按文档层级消歧职责总表`
- `7c41883` `fix(retrieval): 优先闭合限定来源的表格证据`
- `194504d` `test(trace): 对齐证据边界脱敏夹具`
- `03c36ae` `fix(dev): 隔离浏览器视口测试服务`

其中 `6c50c2f` 的层级选择行为已在 `7c41883` 中撤回，保留提交历史以便审计；最终
行为不是两套策略叠加。本文和能力矩阵另作纯文档提交。所有提交只在本地开发分支；
没有 push、merge、force-push、生产部署或真实 Provider 调用。
