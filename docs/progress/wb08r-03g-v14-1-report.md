# WB08R-03G V14.1：检索语义边界闭合实施与真实候选报告

V14.1 最终状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`production_publish_allowed=false`。任务书规定的检索范围下推、文档结构枚举、字段解析、
限定命题证明、部分回答和缓存身份已经落到同一条新链路，并通过了直接相关的离线门禁；
随后只在专用 8289 上构建并运行一个严格候选 C5。

C5 的前四个固定场景通过，但危险时序题在唯一一次后置 `query.interpret` 调用中被当前
Provider 以 HTTP 400 拒绝。该请求前的来源范围、范围内候选、完整文档结构和字段候选均
已形成，因而这是严格 V14.1 路径首次暴露的 Provider 输入合同 P0，而不是 V14 的旧检索
归零问题。按用户规定，出现新问题后没有修改代码、没有创建第二候选，也没有继续运行
Preflight-16 或 A～F。

详细因果链见 [V14.1 P0 清单](wb08r-03g-v14-1-p0-issues.md)。测试身份、证据哈希、运行
边界和清理结果由同目录 `wb08r-03g-v14-1-manifest.json` 固化。

## 任务书对齐结果

本轮把附件作为实施规格参考；执行边界仍以用户本次明确要求为准。没有把附件中的历史
描述或建议候选数当成额外授权。

| V14.1 工作包/不变量 | 实际实现 | 直接证据 |
|---|---|---|
| A：SourceScope 在 `limit` 前生效 | lexical、exact、structural、dense 均接收文档/版本配对范围；空集合 deny；Qdrant 用配对 OR，不做 ID 笛卡尔积；结果后仍保留防御性身份检查 | `memory_vector.py`、`qdrant_vector.py`、`sqlite_fts5.py`、`adaptive.py`、`service.py`；范围外高分干扰、旧版本、空权限和多来源测试 |
| A：已解析文档的有界结构入口 | EvidenceSourcePort 增加 canonical 文档结构枚举与完整性；返回 `complete/next_cursor`；服务层记录结构身份 | `evidence_source.py`、`sqlite_control.py`、`neighbors.py`；真实 DOCX 集成测试 |
| A：候选损失可追踪 | 每个通道记录 scope digest、是否下推、store/ACL/defense/dedup 数量、身份摘要和拒绝分类 | `retrieval.source_scope_candidate_ledger` Trace；C5 危险时序题的 lexical=8、dense=22 且拒绝数全零 |
| B：字段解析独立于限定证明 | 从获准 canonical schema 生成 `FieldCandidate`；字段结果为 RESOLVED/AMBIGUOUS/NOT_FOUND；口语歧义只允许一次后置 `query.interpret`，不增加“准备=输入”全局词表 | `field_resolution.py`、`plan_compiler.py`、`answer_plan.py` |
| B：限定不删除基础字段 | 基础字段选择和 BEFORE/MUST 要求分开冻结；未知限定不会在编译器里覆盖字段身份 | AnswerPlan/coverage 专项测试和 V14.1 语义边界集成测试 |
| C：限定按命题而非词命中 | 证明绑定主体、动作、比较事件、方向、否定与强度；结果为 ESTABLISHED/NOT_ESTABLISHED/AMBIGUOUS | `qualifier_evidence.py`；标题、否定、其他动作/主体/事件及真实正例测试 |
| C：部分回答和文案 | 基础事实可独立发布，限定缺口单独记账；Renderer 不从问句复制“之前/必须”；不回退 legacy Wire | `executor.py`、`grounded.py`、`plan_coverage.py`、`generation_evidence.py` |
| C：缓存不能串资格 | 缓存身份纳入 scope、基础字段绑定、限定要求、结构完整性和策略 revision | `answer_binding_cache_identity` Trace 与专项回归 |
| D：原问走真实新路径 | 集成测试从合成 DOCX 经真实 SQLite/FTS、检索、结构、schema、plan、execute 到 final，不预填正确 AnswerPlan | `test_v14_1_semantic_boundary.py` |

保留了 V14 的 `CompiledAnswerPlan`、`ResolvedQueryView`、`SourceScope`、物理表格事实、D/G
分流、轻量 Wire、服务端投影、真实调用预算、纯覆盖归并和唯一 final。没有换模型、向量库
或索引，没有扩大 token/timeout，没有增加 retry、自审、业务词白名单、case ID 特判或
legacy fallback，也没有启动 04～06。

实现提交：

- `543b4e3edc49345b9505725c44b8e1beaec43602`：
  `feat(retrieval): 闭合 V14.1 来源与语义边界`
- 审计基线：`389ec9e224d86ca81b5268f983b009c03216a035`
- 改动：32 个文件，3924 行新增、314 行删除；新增字段解析、限定命题评估和端到端语义
  边界测试，未重写整个 RAG。

## 真实测试前门禁

严格代码对齐后才构建候选。只运行与 03 问答及 V14.1 改动直接相关的验证：

- V14.1 相关 pytest：`144 passed in 20.20s`。
- 变更范围 Ruff format/check：通过。
- 变更范围 mypy：通过。
- 相关模块 `py_compile`：通过。
- `git diff --check`：通过。
- 当前实现的真实合成 DOCX 端到端测试包含范围内 top-k、文档版本配对、结构完整性、
  字段歧义、限定正反例、部分回答和缓存身份。

没有运行全仓测试、前端、迁移、索引重建或其它与问答功能无关的测试。一个未纳入正式
门禁的旧缓存 fixture 仍使用旧协议而失败；它不是本次 V14.1 直接相关门禁的一部分，未被
宣称通过，也没有为凑通过而修改。

本地第一次直接以整个工作区作为 Docker context 时，在 Docker 读取一个与候选无关且
无权限的历史备份目录前失败，尚未产生镜像。随后从 `543b4e3` 的精确 `git archive`
构建，避免把脏工作区和无关目录带入候选；这是一项构建现场问题，不是产品测试失败。

## C5 冻结身份

C5 在第一条业务请求前冻结为 `business_requests=0`、`provider_calls=0`：

- code SHA：`543b4e3edc49345b9505725c44b8e1beaec43602`
- image tag：`rag-test-wanshitong:wb08r03-v141-c5-543b4e3`
- image ID：`sha256:ce9761f6b2b358d1f539612e8dcd80891777ed39b15734f198e6e84ef9fa3d5e`
- container ID：`23d8542d7df4fb0709282b7606b23d2bdd505f47eea76a5a645e7ca53d3f8055`
- runtime tree：`a4dc85dbcd019fa97e0c8e3824f773719529fd139bfdeae620d7fe781b7fb397`
- runtime guard：`dfbca541a7d784ed2d94e671f3c0f1d1b184ad12eaa82d44fe01ad0b54613e34`
- version bundle：`2f45a961cd9a7094bb7b49ff369472d5015fff94818112dc07611d09f394ce3f`
- asset manifest：`97b313f5058502558589e8dc8a650ef7c6af0813243406d34258fb453d802b03`
- product assets：37 个文件、1107812 bytes
- configuration：`ee6db0601c1fea57936194f868ff508ca28498c893a5c76efcadf19c72f55f47`
- serving fingerprint：`sha256:189907352ec14c09849a0bc05f3beaf8d2352a642c07c2b7fe322f502fe728a8`
- active index：`irev_0329a35ca9ea700133f7b309160118f0`
- dataset：`wanshitong-v2-20260917:wb08r03f-truth-v1`
- dataset SHA：`90ac4be7a81a55016633d91b6c092e36acf83f22528c16bb8ff90de158961568`
- runner SHA：`539cce86acfcbee57f40c11307448a07483758ed1c0d715b4766e6a2463c1115`

镜像只部署到 60 服务器的 `127.0.0.1:8289`。没有编辑部署 `.env` 或 Compose；二者前后
SHA-256 分别保持为 `ce51dd27...5f01` 和 `538e4298...3d5`。生产 8288/18288 没有参与
候选替换。

## 回放取证插曲

第一次以普通用户启动回放时，F024 请求已经发送，但 runner 随后因宿主 Trace SQLite
权限失败：

```text
PermissionError: /data/tyf/wanshitong-wb08r01/data/universal-rag.sqlite3
```

这是 runner 读取宿主取证文件的权限问题，不是候选服务失败。该请求的 Trace
`trace_062b261b68e7ff52cb607e3cdea4b464` 已单独恢复；结果为安全拒答。正式五题批次以
有权只读 Trace 的用户重跑，并显式记录 `prior_request_count=1`。

因此本轮实际业务请求总数是 6：一次捕获中断的 F024，加正式批次 5 次。正式批次自身
没有模型重试，但 safe manifest 的 `retry_count=1` 表示 runner 在已存在一次业务请求后
重新启动整批，不能解释成 Provider retry，也不能隐去。

## C5 正式五题结果

正式批次 5 条均只有一个 `final`：

| 场景 | 终态 | 引用 | 实际关键调用 | 裁决 |
|---|---|---:|---|---|
| F024 | `INSUFFICIENT_EVIDENCE` | 0 | generation=0、interpret=0 | 通过；指定模板正文不能由他文替代 |
| N031 | `ANSWERABLE` | 3 | generation=0、interpret=0 | 通过；安全发布材料清单，无时序/义务注入 |
| 显式来源 | `ANSWERABLE` | 3 | generation=0、interpret=0 | 通过；唯一正确文档和输入字段 |
| 显式列名 | `ANSWERABLE` | 3 | generation=0、interpret=0 | 通过；指定“输入”列，无旧预算阻断 |
| 危险时序 | `INSUFFICIENT_EVIDENCE` | 0 | `query.interpret=1`、generation=0 | **失败；新的 Provider 输入合同 P0** |

不能把 4/5 外推为产品准确率。五题是顺序门禁，不是质量抽样；第五题失败即终止后续门禁。

## 新 P0 的精确因果边界

危险时序题 Trace 为 `trace_215874f50b0ba5f4fad388faedff637d`。失败前已经证明：

1. 来源在分析前解析为 `RESOLVED`，只允许一个文档版本，catalog 完整。
2. 文档结构入口 `entry_available=true`、`complete=true`，返回 22 项。
3. scope 在通道内下推；structural=0、lexical=8、dense=22，ACL、范围、版本、排除和
   身份缺失拒绝数全部为 0。
4. 生成证据 25 条，A1 有 25 条，预生成可用性为 `STRUCTURED_COMPLETE`。
5. canonical schema 形成 4 个完整 `FieldCandidate`，没有因 schema 不完整丢弃。
6. 按任务书只进行一次后置 `query.interpret`；请求为 2 条输入、估算 1377 tokens、
   32 ms 后由 Provider 返回 `HTTP_400 / INPUT_INVALID`，没有 retry。
7. 字段解析因此保守回到 `AMBIGUOUS`，`FIELD_RESOLUTION_PROVIDER_UNAVAILABLE`；AnswerPlan
   保留 BEFORE/MUST 两个限定，但 `field_resolutions=[]`、`selection_digests=[]`，没有生成
   或发布，最终安全拒答。

Trace 中的 `failure_category=PROVIDER_INPUT_TOO_LARGE` 是客户端把 HTTP 输入无效统一映射
后的类别；它不能证明 1377 tokens 真正超限。应用没有保存 HTTP 400 响应体，模型服务
日志也没有给出被拒字段。因此可证实的根因边界只能写到：**当前 Provider 拒绝了 V14.1
后置字段解析请求；具体被拒的 schema 字段或请求体原因未观测（NOT_OBSERVED）**。本报告
不把“某个 structured-output keyword 不受支持”等猜测写成事实。

P0 ID：`P0_V141_FIELD_RESOLUTION_PROVIDER_HTTP_400`。

## 停止门禁

按照“严格计划后出现新问题，不自行修改”的要求：

- 没有修改 Provider payload、schema、提示词、字段解析、fallback、token、timeout、retry
  或测试断言。
- 没有创建 C6，没有第二轮候选部署或真实修复。
- 五题对应变体未运行。
- Preflight-16 未运行。
- EvidencePack-12、CoreAnswer-16、Failed-24、Natural-36、Full-96、Concurrency-4 均未运行。
- A～F 未运行，没有用历史版本结果替代同版本门禁。
- 没有发布生产，也没有启动 04。

## 回滚、生产边界与清理

- 8289 已回滚到 `rag-test-wanshitong:wb08r03f-bd00c56`；最终容器 ID
  `7d8dfc396fdb7af2e7daea991db134cc3fc71c21dd8ef3e0b655c60b4732591d`，image ID
  `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c`，仅绑定
  `127.0.0.1:8289`，`/live` 与 `/ready` 均为 200。
- 生产容器 ID
  `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、image ID
  `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`
  和启动时间 `2026-09-17T01:45:12.001523599Z` 前后相同；60:8288 live/ready 与
  54:18288 live 均为 200。
- 没有修改 Qdrant、活动索引、业务数据、生产 Compose、生产容器或生产镜像标签。
- 60 上的 C5 候选 tag/image、runner、证据中转、镜像归档和构建临时项均已精确删除；
  54 上的中转文件均已删除，候选镜像从未加载到 54。
- 本地 C5 候选 tag/image、构建上下文、镜像/runner 归档和分析临时项已删除；未执行
  全局 prune。
- 服务器证据在本地副本及 SHA-256 验证完成后删除；SSH 会话和 control socket 已关闭。
- 原有 10 个未跟踪评测文件未纳入改动；最终哈希复核记录在 manifest。

## 私有证据和本机分析包

完整归档只保留在本机：

`/home/jerry/work/RAG-private-evidence/wb08r03-v141-20260921/wb08r-v141-c5-evidence-final.private.tar.gz`

SHA-256：`108314029409120cb4212348cb950c2d077bef69d5b07a32b171d0e2cbc69e11`。

为便于用户脱离服务器逐字段分析，已在 Windows 桌面建立：

`C:\Users\jerry\Desktop\湾研院\湾事通\WB08R03_V14_1_C5_P0_分析证据_543b4e3`

其中包含原始归档、解包后的完整 private replay、逐题完整 JSON、授权目标文档 chunks、
一次 runner 权限失败的已恢复 Trace、运行日志/身份、任务书、精确测试源码树、基线到
候选的完整 patch、无第三方依赖的检查脚本和报告。目录含内部问题、答案、引用与文档
正文，不进入 Git；不包含密码、Token 或 `.env` 内容。查看顺序和复核命令见包内 README，
`SHA256SUMS.txt` 覆盖除自身外的全部文件。

## 冻结结论

V14.1 的旧范围归零问题在 C5 中已被实际 Trace 排除，但候选不具备生产替换资格。恢复
工作需由用户另行授权，并首先把 Provider HTTP 400 的具体请求合同作为独立兼容性问题
复现；在原因没有可观察证据前，不应猜测字段、放宽结构化 schema 或用 fallback 掩盖。
