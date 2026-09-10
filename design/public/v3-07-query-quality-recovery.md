# V3-07 查询质量恢复报告

日期：2026-09-11

## 结论

V3-07 已在默认本地 Lane 恢复从问题语义、混合召回、结构证据、最小充分支持集到本地或
Grounded 生成的完整 Product 问答链。公开一次性 holdout、用户指定私有已知回归、
History/Operational Trace 终态和候选容器真实浏览器问答均通过。没有活动远程 Profile 或
模型授权时，系统继续使用明确标识的本地数据面，不把配置阻断或模型失败伪装成“资料中
没有答案”。

本结论不包含真实 Provider 质量、CI、mypy、无关全仓测试、完整离线包、OS 风险批准或
生产部署。它们按用户本轮范围分别保持 `NOT_RUN_BY_SCOPE` 或 `BLOCKED`。

## 恢复范围

### 实际数据面与资料授权

- 查询响应、History、Operational Trace 和管理员 UI 共享本次冻结的 Profile、Index
  Revision、fingerprint、Embedding、向量空间、Reranker、模型、语料授权和预算状态。
- 没有活动 Profile 时明确返回 `default_local_fallback`；deterministic slot 不再显示成
  真实 Dense。
- 新增数据库 migration `0026_corpus_authorization_manifests.sql`。管理员从同源控制台批准
  服务端计算的当前活动语料清单；Revision、活动文档、模型、operation、有效期与累计预算
  均进入不可扩大身份。新版本、删除、恢复或配置变化使批准失效。
- Profile/Revision 不匹配、验证过期、重建中、向量覆盖不全和 Dense 未校准分别展示；
  远程配置无效时本地直接证据链仍可工作且外部调用为 0。

### 查询、检索与证据

- QuerySemantics 覆盖定义、目的、职责、责任角色、事实、枚举、数量、序号、流程与章节
  摘要，并保存标识符、数字、单位、否定、来源限定和日期/版本硬约束。
- 原问始终保留；规则不确定时最多进行一次受控 interpret/rewrite，并只接受一个通过硬
  约束校验的 retrieval-only 变体。
- Exact、FTS、结构事实、Dense、RRF、Reranker 与邻居展开共用 typed semantics。召回
  候选不再被误当成“全部必须支持答案”的集合。
- Evidence 先按对象、关系、表格行列、标题路径、列表组和来源限定选择最小充分支持集，
  再对待发布 claim 做 SourceSpan、数字、单位、否定、数量和版本校验。

### 回答与 Trace

- 新的结构化 renderer 支持定义、目的、职责、责任角色、枚举、数量、序号、流程和章节
  摘要；DOCX 派生编号不会被当成额外列表项目。
- Grounded LLM 只接收最小支持集。模型未配置、未授权、超预算、不可用或结构校验失败
  时，只要支持集完整就发布 `extractive_fallback` 并保留真实退化原因。
- 有效 fallback 在 History 与 Operational Trace 都是 `ANSWERED`；正常无答案为
  `REFUSED`，内部或持久化错误才是 `FAILED`。
- SAFE Trace 记录不含正文和可逆分数的候选身份、选择、原因、语义、数据面和终态；
  DIAGNOSTIC 增加有界 rank/score/contribution/support；FULL 由管理员显式请求并受容量
  preflight 保护。

## 质量证据

冻结公开数据集摘要：

```text
sha256:8441be34b25467eba4076dd23260c072d0311f491b16e1f95aee96a24699d3dc
```

数据集为 18 份公开合成 DOCX、88 个 case，tuning/holdout 各 44，80 个可回答、8 个
不可回答。冻结后候选 holdout 只执行一次：40/40 可答正确、4/4 不可答正确拒答，
answerable accuracy、citation source precision、citation span validity、paraphrase
consistency 与 History/Trace status parity 均为 1.000，错误拒答/错误作答与硬约束违规均
为 0。support-set recall/precision 为 0.91875/1.000。

固定身份基线 `61775e4775d013a8f809e1f2616af376bac7b2a9` 的 holdout Retrieval
Recall@10 同为 1.000，但 answerable accuracy 为 0、错误拒答率 0.625、citation source
precision 0.500、硬约束违规 7。该对照把问题定位在语义、结构支持集和发布层，而不是
用扩大 Chunk 或单纯提高 Top-K 掩盖。

三份独立 tuning 工作区的 P95 中位数比较：DIAGNOSTIC 为 123.724/114.852 ms，候选/
基线 1.077247；SAFE 为 123.856/120.951 ms，比值 1.024018，均在冻结的 20% 门内。
每次 singleflight 8 请求均为 1 leader/7 followers，warm cache hit rate 1.000，远程
Provider 调用 0。

用户指定私有材料只在忽略的本地隔离目录通过正式 Product API 执行。公开聚合为：15/15
已知可答问题命中正确来源、答案和有效引用，另有 5/5 不可回答负例正确拒答；不公开或
打包任何原件、问题、答案、文件名、Trace、数据库或 hash。

详细分母、阈值和外推边界见
[评测与质量声明](../../docs/public/evaluation-and-quality-claims.md)。

## 已执行门禁

以下为本轮实际执行结果，不用其中一类代替另一类：

| 范围 | 结果 |
| --- | --- |
| 查询/检索/回答/评测核心 | 783 passed，1 warning |
| Product API、History/Trace、streaming 组合 | 256 passed，1 warning |
| `scripts/dev.py product-check` | 74 passed；hardcode audit PASS |
| 前端单元 | 92 passed |
| 前端 ESLint、TypeScript、OpenAPI、生产 build | PASS |
| 本机真实 Chromium 桌面/移动 E2E | 13 passed，3 个视口互斥 skip |
| 私有 Product 回归 | 20/20 PASS（15 可答 + 5 负例） |
| 公开冻结 holdout | 44/44 PASS；冻结后只运行一次 |
| 候选 Docker 问答 | 真实上传合成 DOCX、自然问句正确回答、FULL Trace、13 passed/3 skip、重启持久性 PASS |
| Ruff、format、compileall、Google docstring、diff check | PASS |
| 远程 Provider | NOT RUN；外部 HTTP 0 |

候选容器使用正式 `rag-app serve`、SQLite、Qdrant Server 与非 root runtime。浏览器验收按
桌面/移动视口分别执行，中间重启 App 以隔离生产等价的进程内限流桶；不会提高或绕过
生产上传限额。最终 App + Qdrant 重启前后，Project/KB/Document、双槽集合和 point 库存
一致，随后隔离容器、网络和卷全部删除。

## 迁移与重建

- migration 0026 只新增语料授权清单；既有知识库初始状态为未批准，不会自动出网。
- Parser、Chunker、Document/Version/Chunk 身份和原始 Chunk 边界保持不变。
- 本轮查询语义、结构检索、回答与 Trace 改变 serving/cache identity，使旧错误拒答缓存
  失效；没有伪称 Parser/Chunker 版本变化。
- 已有远程 Retrieval Profile 仍按其 fingerprint、向量覆盖和 Revision 校验；若 Profile
  本身改变 embedding 模型、维度或指令，仍必须走新 Index Revision 的影子构建和原子
  激活，不能复用不兼容向量。
- 远程 generation/interpret/rewrite 必须由管理员重新批准当前活动语料与预算；没有
  follow-active 的静默扩权。

## 仍未完成的外部边界

- `LIVE_RETRIEVAL_READY` / `LIVE_GENERATION_READY`：缺少本轮获准的真实 Provider、私有
  数据出网和累计预算运行；保持 BLOCKED。
- CI、mypy、与问答无关的全仓测试、完整 release verify/acceptance 与离线包：用户明确
  暂缓，本报告标记 NOT RUN，不据此阻塞已授权的功能源码合并，也不写成 PASS。
- `PRODUCTION_RELEASE_READINESS`：未执行生产部署；OS 风险责任人批准、Branch
  Protection、真实 Provider 质量与生产容量仍需单独完成。
- `main` 与 `Industry` 保持只读；本轮只允许普通合入并推送
  `feature/universal-rag`，禁止 force-push。
