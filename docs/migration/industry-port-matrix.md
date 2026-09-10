# Industry 通用修复移植矩阵

## 审计边界

- 对比基线：`origin/main@af30f81`。
- 只读参考：`origin/Industry@5cc5d7b`。
- merge-base：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`。
- Industry 独有提交：14 个；三点差异为 107 个文件、29,024 行新增、518 行删除。
- 本阶段没有 merge 或 cherry-pick Industry。混合提交只按下面标明的最小模块补丁重实现。

处置含义：`PORT_NOW` 已在阶段 0 以最小补丁移植；`REIMPLEMENT_LATER` 方向通用但
必须在后续阶段按通用端口重新实现；`REFERENCE_ONLY` 仅保留证据，当前没有独立通用
失败证明；`REJECT_INDUSTRY_SPECIFIC` 只服务工业部署，不进入通用默认路径。

## 逐提交矩阵

| Industry 提交 | 受影响模块与证据 | 处置 | 阶段 0 结果 |
|---|---|---|---|
| `0b1d93d` 新增隔离式工业部署链 | `deployment/industry`、`evaluation/industry`、工业 corpus/bundle 脚本与测试 | `REJECT_INDUSTRY_SPECIFIC` | 不复制语料、镜像、恢复或服务器验收资产 |
| `0b1d93d` 同上 | `scripts/verify_model_contracts.py` 为回答请求补 `question_profile`；`tests/test_release_safety.py` 移除脆弱固定文件数 | `PORT_NOW` | 分别以 `a3a85b0`、`45f08d2` 最小移植并回归 |
| `5ce5870` 工业 simple 部署增量 | `generation/answer.py`、`evidence.py`、`model_contracts.py` 建立显式主体支持门禁；回答测试为合成证据 | `REIMPLEMENT_LATER` | 仅将 `2c4cf22` 所需的通用最小门禁并入 `eb99adf` |
| `5ce5870` 同上 | Industry deployment、smoke 数据、bundle 与 pipeline 工业修订 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `2c4cf22` 精确校验显式来源编号 | 回答校验、fallback、模型合同、pipeline 指纹、合成回答测试 | `PORT_NOW` | `eb99adf` 重实现；覆盖重叠标题、多编号和流式提前发布 |
| `2c4cf22` 同上 | 根级 `BLOCKED.md`、`PROGRESS.md` 工业验收叙述 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `cd5e377` 部署诊断文档 | 工业服务器诊断和剩余验收卡点 | `REJECT_INDUSTRY_SPECIFIC` | 不作为通用运行时证据 |
| `ff8c9d2` UI 与应用更新候选 | `api/ui_session.py`、FastAPI 路由、bearer 前端、Cookie/会话配置 | `REIMPLEMENT_LATER` | 涉及公共 HTTP 鉴权和前端，本阶段禁止改 API，留待独立威胁模型与兼容设计 |
| `ff8c9d2` 同上 | Trace 问题捕获、store/recorder schema、管理 API | `REIMPLEMENT_LATER` | 保留现有 Trace；后续先定义保留期、脱敏和旧库迁移 |
| `ff8c9d2` 同上 | Industry app-update 构建器和部署脚本 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `809fb71` 加固 UI Trace 与应用更新 | `tests/test_app_update_builder.py` 对齐 main 已采用的 simple 三文件实现；runtime fixture 补现有 intent-router 路径 | `PORT_NOW` | `45f08d2`；62 个相关测试通过 |
| `809fb71` 同上 | `api/stream.py`、LLM/evidence/query/cache 的缺失 Google docstring | `PORT_NOW` | `9bcf8e0` 仅补文档，去除 docstring 后 AST 相同 |
| `809fb71` 同上 | UI session、Trace、simple/Industry 部署及设置 | `REIMPLEMENT_LATER` / `REJECT_INDUSTRY_SPECIFIC` | 通用安全方向后续重实现；部署资产不移植 |
| `d5c03cf` serving 更新兼容 | `clients/resilience.py`、Embedding/Reranker/OCR 调用点及韧性测试 | `PORT_NOW` | `cbf1d48`；坏 Content-Type/JSON/schema 仅对非生成式请求有限切换 |
| `d5c03cf` 同上 | UI/Trace/settings 通用方向 | `REIMPLEMENT_LATER` | 需要公共配置和持久化兼容设计，本阶段不改 |
| `d5c03cf` 同上 | Industry serving updater、回滚、自检、last-good | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `8755bf3` 真实服务器更新加固 | Industry compose canonical、权限、镜像和 UI contract checker | `REJECT_INDUSTRY_SPECIFIC` | 属于服务器与镜像验收 |
| `195f9ac` 关闭更新事务缺口 | Industry finalize/update/verify 和事务测试 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `195f9ac` 同上 | `tests/test_target_verifier.py` 两行兼容调整 | `REFERENCE_ONLY` | 没有独立通用失败证据，不单独移植 |
| `e5844e5` legacy last-good 恢复 | Industry last-good/runtime/update 及恢复测试 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `a50d5d5` activation/canary 可恢复 | Industry rollback core、update、last-good 和脚本测试 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `e5c98ce` 不可用目标回滚 | Industry rollback/update/builder 与脚本测试 | `REJECT_INDUSTRY_SPECIFIC` | 不移植 |
| `82e9537` updater 绑定部署基线 | Industry updater/runtime/builder；simple bundle 的镜像复用方向 | `REJECT_INDUSTRY_SPECIFIC` / `REIMPLEMENT_LATER` | 工业实现不移植；通用增量发布合同另立阶段设计 |
| `82e9537` 同上 | Trace 问题捕获兼容测试 | `REIMPLEMENT_LATER` | 与 `ff8c9d2` 的存储兼容方案一并处理 |
| `5cc5d7b` 固化 Industry 源镜像身份 | Industry 源镜像、rollback、last-good、serving selfcheck | `REJECT_INDUSTRY_SPECIFIC` | 完全留在 Industry |

## PORT_NOW 代码与回归证据

| Industry 证据 | 通用提交与最小文件 | 自动回归 |
|---|---|---|
| `0b1d93d` 契约探针问题画像 | `a3a85b0`；`scripts/verify_model_contracts.py` | `tests/test_verify_model_contracts.py` |
| `0b1d93d` 发布清单计数漂移 | `45f08d2`；`tests/test_release_safety.py` | `tests/test_release_safety.py`，改为规则校验而非固定文件总数 |
| `2c4cf22` 显式来源编号门禁 | `eb99adf`；`generation/answer.py`、`evidence.py`、`model_contracts.py`、`api/stream.py` | `tests/test_support_id_answer.py`、`tests/test_answer_streaming.py`、`tests/test_answer_guard.py` |
| `809fb71` main simple/runtime 测试漂移 | `45f08d2`；仅更新现行构建与 runtime fixture 测试 | `tests/test_app_update_builder.py`、`tests/test_runtime_construction.py`、`tests/test_runtime_preflight.py` |
| `809fb71` Google docstring | `9bcf8e0`；5 个回答链源码仅变更 docstring | `scripts/check_google_docstrings.py`，另以去除 docstring 的 AST 相等验证行为未变 |
| `d5c03cf` 非生成式无效响应切换 | `cbf1d48`；`clients/resilience.py`、`model_services.py`、`ocr/client.py` | `tests/test_resilient_http.py`、`tests/test_model_clients.py`、`tests/test_ocr_contract.py`；LLM 未启用无效生成重放 |

## 重点主题结论

- 来源编号与回答引用：`2c4cf22` 的通用 correctness 修复已最小重实现，并用合成证据
  覆盖错误重叠标题、多个编号与流式发布边界。
- 模型响应韧性：`d5c03cf` 的非生成式 failover 已移植；LLM 仍默认不对无效生成
  重放，避免重复副作用。
- UI 会话安全：Industry 方案同时改变 FastAPI 路由、Cookie、前端和部署配置，不能在
  阶段 0 作为“修复”偷渡公共 API，标记为 `REIMPLEMENT_LATER`。
- Trace：问题捕获、保留期和旧库兼容方向有价值，但涉及持久化和管理 API，标记为
  `REIMPLEMENT_LATER`；当前安全 Trace 不被删除或放宽。
- 部署路径：`deployment/industry`、工业 bundle/corpus、服务器更新和恢复资产全部
  `REJECT_INDUSTRY_SPECIFIC`；通用分支不包含这些目录。

## V3.2 / V3-04 复核增量（2026-09-09）

本节不改写阶段 0 的逐提交审计，而是在 V3-00.6 至 V3-04 实际完成后复核当时的
`REIMPLEMENT_LATER` 和新增用户感知能力。冻结远端仍为 main `af30f81`、Industry
`5cc5d7b`，merge-base 为 `af30f81`；远端 Universal 为 `0505fac`，本地开发分支
已前进但未 push、merge 或修改两个只读分支。

### 默认入口与可比性

| 分支 | 固定 SHA | 默认可达路径 | 本轮执行状态 |
| --- | --- | --- | --- |
| main | `af30f81` | `rag-app serve` → legacy app → `/api/chat` / 旧静态页 | 只读源码审计；未恢复旧运行环境 |
| Industry | `5cc5d7b` | `rag-app serve` → legacy app → `/api/chat`，另有 Industry 部署资产 | 只读源码审计；未运行工业服务器、bundle 或 updater |
| Universal 当前分支 | V3-04 行为 `7c41883`，收尾测试至 `03c36ae` | `rag-app serve` → Product Runtime → `/api/v1/...:answer` / React console | 默认 Product 的源码、loopback HTTP/TCP 和浏览器实际验证 |

三者没有在同语料、模型、配置、硬件、预算和缓存状态下执行，因此 main/Industry
作为 A 的质量与时延均为 `NOT_COMPARABLE`，不填 0，也不以旧源码测试替代 Product
证据。

### 用户感知能力矩阵

| 能力 | main / Industry 实际路径与既有价值 | Universal 默认路径与处理 | 决策 | 主要测试/证据 | 未完成事项 |
| --- | --- | --- | --- | --- | --- |
| 默认 Product 生命周期 | legacy `src/rag_app/api/app.py`、`/api/chat`；缺当前 project/KB/document/revision 默认闭环 | `api/p09.py`、`composition/product_runtime.py`、SDK/Application/SQLite/Revision | `KEEP_CURRENT` | `tests/api/test_p09_e2e.py`、`tests/composition/test_product_runtime.py` | 不整体迁回旧 Runtime |
| 原问保留与合法改写 | main/Industry 旧 rewrite 保留原问、失败回原问、最多一条改写 | `QueryAnalyzer` + typed semantics + rewrite guard；原问/硬约束权威，接受改写贯穿下游 | `ADAPT` | `tests/application/retrieval/test_query.py`、`test_rewrite_constraints.py`、`tests/e2e/test_v3_semantic_queries.py` | 真实 LLM rewrite 质量 `BLOCKED` |
| 自然问法语义一致 | 旧路径对首轮普通口语和当前 Evidence 规则不可直接复用 | Analyzer/Planner/Lexical/Dense/Rerank/Evidence/Confidence 共用 `QuerySemantics` | `REPAIR` | `test_synonymous_questions_share_evidence_and_answer`、`test_synonymous_questions_are_equivalent_for_sync_and_streaming` | 开放域覆盖不由有限用例外推 |
| Hybrid 候选 | legacy BM25/Dense/RRF 有可借鉴组合行为 | 当前 Exact/FTS/Dense、主备向量空间、RRF、Rerank；原问/改写按逻辑 family 防刷票 | `KEEP/ADAPT` | `tests/application/retrieval/test_fusion.py`、`test_query.py`、P08 Evaluation | 未授权 Lane B/C 不运行 |
| 来源限定与跨文档同名角色 | Industry 的显式主体锚点方向有价值，但依赖旧 generation/evidence | 动态 source qualifier 在改写中保留；只有唯一 document version 匹配才优先闭合表格 | `REPAIR/ADAPT` | `tests/application/retrieval/test_descriptive_answers.py`、`test_neighbors.py` | 不唯一时仍需用户澄清 |
| Evidence 与引用 | Industry `2c4cf22` 的来源编号门禁已在阶段 0 最小移植 | SourceSpan、node/逻辑表格坐标、完整关系链、Support ID/quote、active revision 校验 | `KEEP/REPAIR` | `test_evidence_precision.py`、`test_evidence_table_coordinates.py`、`test_grounded_claim_validation.py` | 真实生成质量未测 |
| 正确/完整回答与合理回退 | 旧 generation/extractive fallback 可作行为参照，默认入口和缓存不同 | 模型 claim 校验后发布；无配置/授权/预算/失败可显式摘录回退；无 Evidence 拒答 | `ADAPT` | `tests/api/test_grounded_product.py`、`test_descriptive_answers.py`、`tests/e2e/test_v3_semantic_queries.py` | 真实 LLM 分母 0 |
| 错误拒答与无依据作答 | Industry 主体支持可防部分无依据输出 | 数字/单位/否定/范围/时序/版本/关系硬约束，Dense 近似不能单独变成答案 | `KEEP/REPAIR` | `test_unsupported_relation_is_refused`、`test_grounded_claim_validation.py`、P08 4 个 negative | 未见自然语言仍需新 holdout |
| 配置与实际调用可见 | 旧 UI/Trace 路径不可直接放入 Product | `result_origin`、本次 generation/rewrite 调用、cache/budget/auth/provider 状态分别展示 | `ADAPT` | `tests/api/test_grounded_product.py`、`frontend/src/pages/query-related.test.tsx` | 不把配置存在写成实际调用 |
| DOCX 通用结构 | main/Industry 无当前 OOXML v4 + IR + Chunk v3 全合同 | 纯空白、表格、列表边界修复，正常三视图/顺序/SourceSpan golden 保留 | `KEEP/REPAIR` | `test_docx_contract_v3_00_6.py`、`test_docx_whitespace.py`、`test_v3_00_6_product_ingestion.py` | 非穷举 Word 生产器 |
| legacy DOC 图文 | Industry 有 LibreOffice 转换方向，但无当前 Artifact/Revision 沙箱合同 | 受限 DOC→清洗 DOCX→当前 Parser/IR/Artifact/Chunk；source/derived/media 分离 | `ADAPT` | `test_doc_conversion.py`、`test_word_document.py`、`test_v3_01_doc_media.py` | 原格式媒体盘点可为 partial/unknown |
| OCR | Industry 有本地 OCR revision/Bearer/限额/bbox/confidence 价值 | 统一 local/remote Port、来源类型、限额、cache identity、新 Revision；默认可关闭 | `ADAPT` | `tests/application/test_ocr_enrichment.py`、`test_product_ocr_adapters.py` | `OCR_LIVE_QUALITY=BLOCKED` |
| 图关系 | 无可直接搬回的完整 Product 合同 | candidate/direction/ambiguity/review/occurrence；接纳后新 Revision 才可作为证据 | `SAFER_EQUIVALENT` | `tests/application/test_diagram_relations.py`、`tests/evaluation/test_v3_02_baseline.py` | `DIAGRAM_RELATION_LIVE_QUALITY=BLOCKED` |
| Operational Trace | Industry Trace 的层级、候选、Provider span 方向有价值 | Product 双存储共享公开 trace ID；权限、TTL、导出、备份、fail-soft/full fail-closed | `ADAPT` | Trace store/API/UI/backup tests；V3-02 固定 Trace baseline | 真实生产容量未测 |
| 真流与首内容 | Industry legacy `/api/chat` 有 NDJSON 流行为，但不满足 Product scope/claim 合同 | `rag-answer-sse-v1`；真实 TCP 在 Provider 未结束时发布已校验完整 claim | `ADAPT` | `test_tcp_streams_headers_and_validated_claim_before_provider_finishes`、`test_aliyun_chat_stream.py` | 真实 Provider TTFC `BLOCKED` |
| 浏览器增量与停止 | 旧静态页不是当前 React Product | 增量暂存、唯一 final、AbortSignal、导航/KB 切换丢弃迟到事件 | `REPAIR` | `frontend/src/api/client.test.ts`、`frontend/e2e/console.spec.ts` | 生产代理缓冲未测 |
| 取消、成本与撤权 | 旧路径无当前 QueryExecutor/History/Trace 一致结算合同 | 断连传播、不可中断占槽、实际 usage、无成功缓存、来源/Session/Token 在途重查 | `SAFER_EQUIVALENT` | `test_real_tcp_disconnect_keeps_slot_and_never_commits_success` 及三项撤权竞态 | 真实远程取消浪费分母 0 |
| Industry updater/rollback/last-good | Industry 专用实现完整且与服务器布局绑定 | 不进入通用默认路径；只保留未来通用发布设计线索 | `DEFER/REJECT_INDUSTRY_SPECIFIC` | 阶段 0 逐提交审计 | 不属于本轮必修能力 |

### V3-04 选择性继承结果

阶段 0 的以下 `REIMPLEMENT_LATER` 已在当前 Product 中完成为通用实现，而不是复制
Industry 模块：

- UI 会话、CSRF、scoped token 和 Product API 由 P09/P10/P11 路径建立；
- Operational Trace 在 V3-02 用 Product 双存储、保留期、权限和导出合同重实现；
- generation/evidence 的显式主体与来源支持原则已经适配到 typed semantics、
  SourceSpan、claim validation 和 source qualifier；
- 真流、停止和成本结算在 V3-03 以 Product SSE 和 QueryExecutor 重实现。

以下项目继续保持原判定：Industry `deployment/industry`、工业 corpus/bundle、服务器
updater、rollback、last-good 和发布 journal 不进入通用默认产品。它们不是本轮
DOCX、自然问法、图文、Evidence 或流式必修项的替代品。

### 非回归证据与剩余限制

V3-04 在相同公开合成数据、Parser/Chunker、`fts5-only` selected config 和离线
Provider 边界下，对比修复前 `a760637` 与修复后 `7c41883`。Recall@5 均为 1.0；
修复后 answerable accuracy、refusal F1、Citation document/chunk/validity 和
Source range recall/precision/coverage/F1 均为 1.0，16 个 gate 全通过；安全计数
仍全部为 0。详细 A/B/C、私有匿名聚合、holdout 复用限制及重建范围见
`design/public/v3-04-unified-acceptance.md`。

这只证明当前冻结回归集的选择性继承与非回归。main/Industry 仍为
`NOT_COMPARABLE`；真实 LLM/OCR/视觉、生产代理、容量和发布安全批准没有由源码
矩阵推断为完成。
