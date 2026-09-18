# WB08R-03F 来源真值 v1

本目录只用于候选评测。24 条身份来自冻结的 Formal 6（F015、F017、
F037、F040、F041、F048）和 Natural 18（N043–N060）。
`cases.ndjson` 固定问题摘要、预期行为、事实编号和最小来源要求；
`evidence_truth.ndjson` 保存逐字引文及活动 SourceSpan 身份；
`manifest.json` 固定文件摘要、知识库与索引版本。
问题正文仍从 `evaluation/wanshitong/v2/` 冻结题集读取。

2026-09-18 只读审阅 60 服务器 8289 候选容器
`wanshitong-wb08r01-app` 的 `/data/universal-rag.sqlite3`，
使用 `mode=ro` 查询活动知识库、文档版本和索引
`irev_0329a35ca9ea700133f7b309160118f0` 的 2880 个 Chunk。
每条 Gold 引文均逐字位于同一活动文档版本的可引用 SourceSpan 内；
表格项另记录表、行、列和来源节点。旧候选引用只用于定位线索，
未把旧模型答案当标准答案。真值是该索引版本的快照；
索引或活动文档版本变化后，必须重新审阅并另建修订。

19 题已核对 Gold Support。5 题保留 `NEEDS_TRUTH_REVIEW`：
F017、N048、N057 涉及Ⅱ级事件表格的跨行合并与分段引用；
N050、N058 涉及首单交付的跨阶段职责完整边界。
这 5 题不计入功能通过率的分母，也不能算作正确拒答。
N055 的文档列出引入前的技术能力审核与协议起草，
但未证明问句暗示的先后关系，因此评测期望为 `LIMITED`。
其余已核对题的行为和事实范围见 `cases.ndjson`。

所有 `case_id`、冻结问题、Expected Source、Gold 引文和事实编号
只能由评测代码读取。`.dockerignore` 与
`Dockerfile.dockerignore` 排除 `evaluation/wanshitong/**`；
`src/`、`frontend/src/` 和运行时 Prompt 不得导入或复制这些数据。
生产 Trace 只记录真实候选来源身份，不记录 Expected Source 或 Gold。

评测程序位于 `evaluation/wanshitong/v2/run_wb08r03f_candidate.py`。
它只接受本机 8289 候选端口，并从候选的 SAFE
`retrieval.generation_evidence` 事件计算来源和 Gold Span 入包情况。
在 60 服务器宿主机运行时，Trace 可通过
`--trace-container wanshitong-wb08r01-app` 只读取得；不能误用
`product-traces.sqlite3`。在可直接读取 SQLite 的环境，
可传 `--trace-db /data/universal-rag.sqlite3`。

评测只证明当前快照中的来源与已审阅事实。索引覆盖、引文存在和
引用数量本身不证明 Claim 蕴含或答案完整性；真实回答仍需结合
逐 Claim 审阅与 CoreAnswer、Natural、Full Gate。

runner 的 `--gate` 支持 `evidence-pack-12`、`core-answer-16`、
`failed-24`、`natural-36`、`full-96` 和 `concurrency-4`。
CoreAnswer-16 由 14 个应答题和 2 个拒答控制题组成；Natural-36
由 30 个自然问法和 6 个对抗控制题组成。并发 Gate 使用四个独立
匿名会话、各自的会话 ID 和屏障，同时核对 Final、Trace 原问摘要、
History 原问摘要及匿名 owner 摘要。所有输出文件必须写到
`evaluation/wanshitong/v2/results/`；`--trace-container
wanshitong-wb08r01-app` 在 60 服务器宿主普通用户无权读取主库时
通过容器内只读 SQLite 查询。

Gate A 对 F015、N056 还核对同表其它行是否被关联到目标 Atom。
结构观测为 `PARTIAL` 或缺失行坐标时返回 `NOT_OBSERVED`，不会把
空计数判作零污染。Gate C 的 N049 是目录存在性捷径，单列核对
唯一目录来源引用、非空应答及人工正文越界审阅，不计 Evidence
Pack Recall 分母，也不伪造 Claim。

CoreAnswer-16、Failed-24、Natural-36 和 Full-96 的严重事实、
错误来源、模板正文和结构串答需要人工逐答案审阅。可用
`--review-judgments evaluation/wanshitong/v2/results/<文件>.ndjson`
输入审阅结果；每行包含 `case_id`、`question_sha256`、
`answer_sha256`、`reviewer` 和四个非负整数：
`unsupported_high_risk_fact_count`、
`wrong_source_severe_error_count`、`template_body_overreach_count`、
`structural_sibling_error_count`。Full-96 因有重复 case ID，
每行还必须包含 `run_id`。私有结果文件提供当前问题、答案及其
摘要用于核对；未审阅或摘要失配时 Gate 明确显示 `NOT_REVIEWED`。
