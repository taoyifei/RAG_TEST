# WB08R-03G-P0 冻结证据与第一断点报告

## 1. P0 结论

本微阶段结论为 **BLOCKED**。N031 已定位到“Gold 支持在固定索引中存在，
但三条 Gold Chunk 均未进入 `rerank.input`”这一最早可观察断点；N033 已定位到
“同意图来源 Chunk 进入 rerank 后被 `assemble_evidence` 以
`INCOMPLETE_EVIDENCE_GROUP` 拒绝，最终一条模型 Claim 又被映射为
`CLAIM_SUPPORT_NOT_OWNED`”这一双层断点。N033 的原始 Claim、Quote、所选
Support ID、Atom 允许集合、原始拒绝码和 fallback 失败分支均未被历史 Trace
保存，因此不能把任何猜测码写成线上根因。新增的受控私有草稿记录器只能在未来
一次 8289 候选重放中取得这些字段，不能倒推或改写这次历史结论。

P0 没有修语义召回、Gold、业务同义词、引用规则、校验阈值、生成 Prompt 或
预算，也没有运行 04、部署候选、请求生产服务或清理镜像。WB08R-03 保持
`PAUSED`，`merge_allowed=false`。

阻断 P0 完整验收的输入和环境如下：

1. 工作区、`C:\Users\jerry\.codex\attachments`、Downloads、Desktop、
   Documents 和 `/home/jerry` 均未找到本包 01 审计报告或
   `90_contract_probes.py`。当前 Goal 正文可作为 02 阶段计划，但不能替代缺失
   的 01 审计事实和 90 固定反例。
2. 当前获授权配置和环境没有可用服务器认证。对现有服务器地址的批处理 SSH
   只读连接返回 `Permission denied (publickey,password)`；未搜索、复制或恢复
   其它凭据。
3. 因而本轮没有对候选库先执行 Schema 检查，随后查询 `query_history` 和
   `query_trace_events`。历史 SAFE 摘录可以用于冻结已观察事实，但不能冒充本轮
   直接数据库取证。

安全机器清单见
`docs/progress/wb08r-03g-p0-manifest.json`。

## 2. 证据等级和来源

- **事实**：当前仓库命令、冻结数据文件、状态文件、历史运行的 SAFE 输出或
  私有结果中直接存在的值。
- **推断**：由两个以上事实得到，但历史 Trace 没有直接记录中间对象或因果关系。
- **NOT_OBSERVED**：历史运行没有保存，或本轮不能从对应候选数据库重新读取。

本报告没有用当前 HEAD 的实现反推旧镜像行为。旧运行行为只取自
`bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` 的候选身份、历史结果与 SAFE
Trace 摘录；当前代码只用于复核通用合同和增加后续可观测性。

## 3. 冻结身份

### 3.1 Git 与工作区

| 项目 | 冻结值 | 证据等级 |
| --- | --- | --- |
| 仓库 / 分支 | `taoyifei/RAG_TEST` / `codex/wb-08r-adaptive-rag` | 事实 |
| 本轮开始 HEAD | `661498176a2c885f7a109c0af0024ae2ad59b88f` | 事实 |
| 审计基线 | `661498176a2c885f7a109c0af0024ae2ad59b88f` | 事实 |
| 历史运行代码 | `bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` | 事实 |
| `bd00c56..6614981` | 仅 `wb08r-03-issues.md`、`wb08r-state.json` | 事实 |
| 原有未跟踪评测文件 | 10 个，均保留且未纳入本轮修改 | 事实 |

`bd00c56` 与审计 HEAD 的 `grounded.py` SHA-256 均为
`14617067898ec8cd7e3cc44d81c759616fbe13732678bc6c4268de602975be1f`；
因此本轮对旧回答合同的代码复核没有使用后来改变的 `grounded.py`。

### 3.2 候选、配置和数据

| 项目 | 冻结值 | 证据等级 |
| --- | --- | --- |
| 候选镜像 | `rag-test-wanshitong:wb08r03f-bd00c56` | 事实 |
| 镜像 ID | `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c` | 事实 |
| 历史容器 / 端口 | `wanshitong-wb08r01-app` / 8289 | 事实 |
| 镜像源码修订 | `bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` | 事实 |
| 产品资产 Manifest | `927660b0864b710719c8af4d6911e22087c63baef1587f783d3c895482b09f72`，37 文件，1,107,812 字节 | 事实 |
| Compose 摘要 | `3257dfd81ee5b1364f21f9f74638b9103bab449f3e0850b76285d91298bbbf5d` | 事实 |
| Pipeline 摘要 | `037e2960634c2314df47cc53f6212224035cd4b1e9c48e189a8df5d595bf5570` | 事实 |
| Product/Profile | `RAG_PRODUCT_MODE=wanshitong`，内网模型组合 | 事实 |
| Provider / 模型 | OpenAI-compatible；`Qwen/Qwen3-8B-AWQ` | 事实 |
| 结构化输出 | `response_format`，显式关闭 thinking | 历史记录事实 |
| Dataset | `wanshitong-v2-20260917` | 事实 |
| Runner SHA-256 | `beb4e0fe5a70bba18225fa7926101c3d274ec4ee41a5b423e4a33a897babc1d9` | 事实 |
| Dataset Schema SHA-256 | `67ea379f12a15a8cd56a63b254ba90448a85a6297d95c7c4c558757c9f4ea0d6` | 事实 |
| Active index | `irev_0329a35ca9ea700133f7b309160118f0` | 冻结真值事实 |
| Active snapshot | `9fba9aac42c3482c36e266e1bf2a04557962c11a5073f38a95d1e1407b5bae06` | 冻结真值事实 |
| KB | `kb_328756522df5f70ad9a852fdb5fea3b5` | 冻结真值事实 |
| 候选实际挂载 | `NOT_OBSERVED` | 未观测 |

生产容器为 `wanshitong-app`，已有直接 Docker 映射记录是 **8288 → 8088**。
本轮没有把 18288 当宿主回环端口，也没有修改生产容器。当前服务器不可访问，
所以本轮无法重新执行 `docker inspect`；生产挂载和当前镜像身份标记为
`NOT_OBSERVED`，保留历史状态文件中的只读基线。

## 4. 数据库取证边界

对应历史数据库路径记录为 `/data/universal-rag.sqlite3`。本轮没有可用服务器认证，
因此没有机会按“先看 `sqlite_master` / `PRAGMA table_info`，再执行固定 SELECT”的
顺序直接读取同库。以下两点必须分开：

- **事实**：历史运行工具曾从名为 `query_history` 和 `query_trace_events` 的对象
  得到 SAFE 摘录；N031 留下了
  `trace_6c780ebd91b5b419110e8a72c993abff`。
- **NOT_OBSERVED**：本轮未重新确认候选库实际表和列；N033 的 trace ID 没有进入
  保留下来的安全结果，不能从当前材料恢复。

新增的 `scripts/wb08r03g_private_replay.py` 固定使用 SQLite `mode=ro`，先检查两张
表及必需列，再按参数化 `trace_id` 读取；没有任意 SQL 输入或更新路径。它只允许
`127.0.0.1:8289`，固定 N031、N033、F015、F013 四题，每题一次、零重试，私有
结果必须写到仓库外目录。

## 5. N031 原始证据链

### 5.1 Gold 支持身份

N031 冻结数据指向 `WB08R-F-015`，意图身份为
`legacy/DOCX-043-Q2/4c52a4f4674d`。配对冻结真值来自文档版本
`dver_73e6ecf5f4678f09da2b2284e6099db5`，同一表第 2 行：

| Gold | Chunk | Node / 表坐标 | Quote SHA-256 |
| --- | --- | --- | --- |
| G01 行名 | `chunk_5d476cdffac74dc3d578f87d851fdb75` | `node_e97185257088b1bb899332012a01fa8e`；`body/tbl:2/tr:2/tc:0/p:1` | `2b8357b4cef4362574eee1bf1b594a5fb7840af7be0f4128092720ce5aebcc6c` |
| G02 输入 | `chunk_39b41e0c274e2873812c8da6c0a37189` | `body/tbl:2/tr:2/tc:2/p:1` | `54dc6932a131875dc5709ded1a2a5addec5c7579ea1ecb1165f366a1df39b7bb` |
| G03 启动条件 | `chunk_a8e60e79651e4ab437adf67ebabe2e37` | `body/tbl:2/tr:2/tc:4/p:1` | `9b41bbcd6f14edf0e863ee5ff801f66a0a7adc12a48e468c9514129bb6cba81c` |

这些身份来自固定 active snapshot 的人工真值。它们证明索引快照中存在可引用节点，
不证明旧候选运行的任一召回通道实际返回了这些节点。

### 5.2 阶段链

| 阶段 | 历史观察 | 判定 |
| --- | --- | --- |
| 解析 / 索引 | 三条 Gold 在固定 snapshot 有 Document/Node/Chunk/表坐标 | 事实；旧请求的解析动作本身未重跑 |
| Root / Atom 召回 | structural 24、lexical 48、dense 24 | 只有计数；各通道 Chunk 身份 `NOT_OBSERVED` |
| Fuse | 35 个候选 | Gold 是否曾进入 fuse `NOT_OBSERVED` |
| Rerank input | 24 个 Chunk，三条 Gold 全部不在集合中 | **最早可观察缺失** |
| Pre-pack / assemble | retrieval 39、model evidence 16、answer support 0 | Gold 已无法在此链中恢复 |
| Source-node closure | 新增 0 | 没有恢复 Gold |
| Corrective retrieval | `NO_GROUP_NEIGHBOR`，新增 0，post `PARTIAL` | 没有恢复 Gold |
| Sent-pack | `NOT_OBSERVED` | 历史 Trace 未保留最终包 |
| 模型选择 | 1 次生成；历史最终接受 S1/S2/S3/S4/S8 | 均来自替代文档 |
| 校验 / 发布 | 1 个生成 Claim 被恢复为 2 个已接受/已发布 Claim；5 个引用 | 本地 Claim 门通过，但冻结来源门失败 |

**第一断点结论**：N031 的第一可观察断点是
`GOLD_SUPPORTS_ABSENT_AT_RERANK_INPUT`。准确断点只能收窄为“各召回通道、融合或
rerank 输入裁剪之一”；因为历史 Trace 只有通道计数，没有通道 Chunk 身份，不能
继续猜是哪一个通道先丢失。

### 5.3 实际替代引用与事实适用性

5 条引用都来自同一替代文件（私有相对路径 SHA-256
`c74d5370983d61adb42d5ac8aacac4ccc2fe948edd4b5ff026cf66a9e030bf25`），
不是冻结 Gold 文件（私有相对路径 SHA-256
`273af4d763a524c9cf1ade71f8cd95ec3f7d6da84aac0a70e3da26c1ecae9353`）：

| Quote SHA-256 | 私有 Locator SHA-256 | 事实适用性 |
| --- | --- | --- |
| `67d182aaa71f42c676ee17662a1c044989f647550521e7f4b8009611f73a60a5` | `c1a73d9112c5a9611018bc057f2cc7259e98abeea26eda87505a445994027e63` | 主题相关；不能证明冻结模式行中的输入与启动条件 |
| `2c50f319178f0a3827e3bf4ca485e89cfb34933bc6674db2b1e0cac835ed5759` | `f214fcde8d56381ef8f0817d7cbb5e5e616363c1cada87688b4eeff0966870bf` | 可支持替代文件自身的要求；不能替代冻结模式行 |
| `082257c3edd8670d0a7fd89b83955bb1adff7e476258dc4f2130e3b62d15f13b` | `f214fcde8d56381ef8f0817d7cbb5e5e616363c1cada87688b4eeff0966870bf` | 同上 |
| `8ad95e3b4cf6cb5b7ed593ba2ef7ac68dac9055ec6aea9954ad97bf59e87fc6a` | `f214fcde8d56381ef8f0817d7cbb5e5e616363c1cada87688b4eeff0966870bf` | 同上 |
| `2169acb329529788475b983370700501cfac9fe722fdbccbf9d800a725ca39ba` | `f214fcde8d56381ef8f0817d7cbb5e5e616363c1cada87688b4eeff0966870bf` | 同上 |

5 个替代引文哈希均不等于任一 Gold Quote 哈希。字符交集只说明主题邻近，不能证明
事实蕴含；因此本报告保留“主题相关”的推断，同时保持冻结来源 Gate 为失败。

## 6. N033 原始证据链

### 6.1 真值边界

N033 的冻结数据指向 `WB08R-F-013`，意图身份为
`legacy/DOCX-041-Q2/2f2dbf114c12`，预期文件的私有相对路径 SHA-256 为
`183d4b48104f7890d73a17a2348661f6fd1d829b0f8acdd6f95b7c23c285a64f`。
但 `wb08r03f-truth-v1` 没有 N033 或 F013 的直接逐字 Gold；历史 runner
也把 N033 记为 `NEEDS_TRUTH_REVIEW`。因此不能把 N050/N058 的逐字真值直接改名为
N033 Gold。

可用于定位的同意图比较身份如下：

- `chunk_2ef52e5f6a5bf335c23f85d7bd8d5e42`：同一文档版本，
  `body/tbl:13/tr:1/tc:1/p:2`，Node
  `node_107f296265bcc9289d4938a6a6694b11`。
- `chunk_eff310e5b592ec8cf0c3905039193bea`：同一文档版本，
  `body/tbl:22/tr:1/tc:1/p:8`，Node
  `node_429328e0a4f1cff7410a65b72ed2f16c`。

二者同文档版本，但不是同一 Node、同一表或同一表行。表 13 的准备阶段与表 22 的
责任终点/角色转换存在跨阶段、跨角色边界；来源恢复不能把它们自动拼成一条 Claim。

### 6.2 阶段链与第一断点

| 阶段 | 历史观察 | 判定 |
| --- | --- | --- |
| Root / Atom 召回 | structural 24、lexical 48、dense 24、fuse 48 | 通道身份 `NOT_OBSERVED` |
| Rerank input | `chunk_2ef...` 位于 24 个输入中的第 9 位 | 同意图来源已到达 rerank |
| Pre-pack / assemble | retrieval 61、model evidence 16、answer support 0；`chunk_2ef...` 被标为 `INCOMPLETE_EVIDENCE_GROUP` | 第一处可观察合同降级 |
| Source-node closure | 新增 `chunk_eff...` | 新增项不是 `chunk_2ef...` 的同 Node/同表行连续片段 |
| Corrective retrieval | `CORRECTION_NOT_TRIGGERED`，新增 0，post `MISSING` | 未形成 Answer Support |
| Sent-pack | `NOT_OBSERVED` | 无法确认最终 S-ID、来源组与 span |
| 模型 | 1 次生成，1 个 NaturalClaim | Claim 正文、Quote、所选 S-ID 均 `NOT_OBSERVED` |
| 校验 | 接受 0；公开分布 `CLAIM_SUPPORT_NOT_OWNED: 1` | 原始码与具体 validator `NOT_OBSERVED` |
| 发布 | `NO_ANSWER`，发布 0 | 终态拒答 |

N033 有两个需要分别记录的断点：

1. **第一可观察合同降级**：同意图来源进入 rerank 后，在 assemble 阶段被当成不完整
   来源组，`answer_support_count=0`。
2. **终止发布的断点**：模型确实返回 1 个 Claim，但 Claim 校验只留下公开映射码
   `CLAIM_SUPPORT_NOT_OWNED`。历史 Trace 没保存原始码，不能确认它来自
   `CLAIM_SUPPORT_OUTSIDE_ATOM`、`CLAIM_SOURCE_MISMATCH` 或其它路径，也不能证明
   assemble 降级必然导致最终拒绝。

### 6.3 Repair 与 fallback

**事实**：`GroundedAnsweringService._answer_with_plan` 只在
`omitted and accepted` 为真时调用局部 repair。N033 `accepted=0`，所以
`repair_calls=0`；这不是“repair 尝试失败”，而是条件没有进入。

**事实**：接受列表为空后，代码调用 `_safe_extractive_fallback`。历史 Trace 只记录
`extractive_fallback_used=false`，没有记录哪个 `return None` 分支触发。

**NOT_OBSERVED**：N033 是因无安全摘录、问题锚点缺失、来源等级不匹配，还是其它
fallback 分支失败，历史材料不能回答。

## 7. 通用合同缺陷与最小反例

### 7.1 原始拒绝原因被公开映射合并

函数：`_natural_rejection_code`。

`CLAIM_SOURCE_MISMATCH` 和 `CLAIM_SUPPORT_OUTSIDE_ATOM` 都映射为
`CLAIM_SUPPORT_NOT_OWNED`。历史 `consume` 只累加映射后的计数，导致两个不同合同
失败无法区分。N033 的原始原因因此是 `NOT_OBSERVED`，不能补猜。

真实应用链最小反例：A1 只允许 S1，模型却为 A1 选择分配给 A2 的 S2。改动前定向
测试因 `GroundedOutcome` 没有原始诊断字段而失败；改动后得到：

- raw：`CLAIM_SUPPORT_OUTSIDE_ATOM`
- public：`CLAIM_SUPPORT_NOT_OWNED`
- validator：`_validate_natural_atom_support_scope`
- selected：S2；allowed：S1
- Claim 与 Quote 只存 SHA-256，不存正文

对应回归：
`test_rejection_diagnostic_preserves_raw_atom_scope_reason`。

### 7.2 accepted=0 不进入局部 repair

函数：`GroundedAnsweringService._answer_with_plan`。

同一真实应用链反例确认 accepted=0、`repair_calls=0`。本 P0 只观测该条件，没有改变
repair 语义或预算。

### 7.3 fallback 的 `None` 没有历史原因

函数：`_safe_extractive_fallback`。

合成证据与问题没有安全相关摘录时，函数返回 `None`。改动前没有诊断出口；改动后
同一真实函数返回行为不变，并记录 `NO_SAFE_EXCERPT`。其它稳定分支包括
`NO_LINKED_SUPPORTS`、`NAMED_ROW_REQUIRED`、`SOURCE_LEVEL_MISMATCH`、
`NO_CITABLE_COMPLETE_EXCERPT` 和 `QUESTION_ANCHOR_MISSING`。

对应回归：`test_failed_fallback_records_safe_failure_reason`。

### 7.4 来源组、连续 Node 与表格行不是同一合同

真实函数回归分别证明：

- 同一 Claim 选择多个来源组时，`_validated_source_group_claims` 逐组恢复并重新校验；
- 恢复后的另一明确主体仍会被丢弃；
- 同一 Node 连续片段可以重新拼回完整原句；
- 命名且完整的表格行可作为 fallback，不能用任意同文档片段替代。

这些反例不能证明 N033 实际选择了哪些支持；N033 的 selected S-ID 和 source span
仍为 `NOT_OBSERVED`。

### 7.5 缺失的 90 合同探针

`90_contract_probes.py` 不在附件、仓库或本轮允许搜索的常见目录中，所以没有执行，
也没有伪造它的输出。本轮另外执行的是仓库真实函数/应用链回归，不把它冒充 90
脚本或端到端线上测试。

## 8. 仅观测改动

本轮在内部 `GroundedOutcome` 和 SAFE Trace 增加：

- `claim_rejection_diagnostics`：Atom、raw/public code、validator stage/name、所选与
  允许 Support ID、Claim/Quote SHA-256；
- `extractive_fallback_reason`：fallback 成功或最后一个稳定失败分支。

这些字段不含问题、Claim、Quote 或证据正文。已有公开 REST path、请求/响应和
History Schema 未变；SAFE Trace 只兼容追加上述可选字段，已有字段语义未变。
排名、准入、阈值、Prompt、Provider 选择、repair 次数和预算行为未变。

历史记录没有保存 N033 的实际 NaturalClaim 和 Quote，因此在提交
`f9599dd4a1c42ee1c2cc9e7743826ccfe8599b5e` 中增加了默认关闭的
`PrivateReplayDraftRecorder`：

- 只有目录、确认值和捕获上限三个显式环境变量同时存在时才启用；普通启动返回
  `None`，沿用原来的 `ProductGroundedModel` 构造路径；
- 目录必须是绝对路径、已存在、不是符号链接、属于进程 UID，且组与其他用户均无
  权限；输出固定为 0600 的 `raw-generation-drafts.ndjson`，旧文件非空时拒绝启动；
- 每次记录有显式 1～32 条上限，单条上限 2 MiB；超限失败，不静默覆盖；
- 同步与流式生成都在 Provider 返回结构化 `AnswerDraft` 后、业务 Claim 校验前记录
  实际 `GenerationRequest` 和 `AnswerDraft`。因此私有记录包含 sent-pack 对应的
  Evidence、SourceSpan、Atom 允许集合、NaturalClaim、Quote 与所选 Support ID；
- 原始内容只写到仓库外的受控文件。SAFE Trace 和 SAFE manifest 仍只保存 ID、摘要、
  原因、条数和 SHA-256。

重放脚本新增必需参数 `--raw-drafts`，按每题请求前后的字节偏移关联新增草稿；
N031/N033 没有捕获草稿时立即失败，每题超过两条时也失败。四题仍各发一次、零重试，
且每题完成后立即将私有结果 `fsync`，中途失败不会丢失已取得的诊断。该工具已在本地
离线验证，未部署到 8289，故 N033 的历史原始 Claim 继续标记 `NOT_OBSERVED`。

受控 replay 源码会在读 Trace 前核验实际 Schema，只运行固定四题并将 raw 结果写到
仓库外。历史私有包位于：

`C:\Users\jerry\AppData\Local\Codex\diagnostics\wb08r03g-p0-20260919\historical-private-replay.json`

文件 SHA-256 为
`ec37da2f79990736ba2678d03b99ef990f5082463299df339ba9fd6a72bad4ac`，
45,152 字节；ACL 仅当前用户与 SYSTEM 完全控制。仓库 Manifest 不含正文。

## 9. 已执行验证

先失败证据：

```text
pytest ... -k "rejection_diagnostic... or failed_fallback..."
2 failed
- GroundedOutcome 尚无 claim_rejection_diagnostics
- _safe_extractive_fallback 尚无 diagnostic_reasons

pytest tests/product/test_private_replay_capture.py
collection error: ModuleNotFoundError: rag_app.product.private_replay

python scripts/wb08r03g_private_replay.py --help
ModuleNotFoundError: evaluation
```

改动后执行：

```text
pytest 六个真实函数/应用链合同反例
6 passed

pytest 两个新增回答观测用例
2 passed

pytest tests/evaluation/test_wb08r03g_private_replay.py
4 passed

pytest 回答、Trace、runner 与离线 P07 相关范围
151 passed

pytest 私有草稿、受控 replay、Claim 与离线 P07 相关范围
64 passed

ruff check 本轮四个实现/脚本文件和两个新增/修改测试文件
All checks passed

python -m py_compile 本轮四个实现/脚本文件和两个新增/修改测试文件
passed

python scripts/wb08r03g_private_replay.py --help
passed

JSON 解析：wb08r-state.json、wb08r-03g-p0-manifest.json
passed

git diff --check
passed
```

一次包含 `test_atom_channels_merge_before_one_rerank` 的扩大范围为
`98 passed, 1 failed`。失败是该既有测试期待中文逗号/问号原样进入 dense batch，
实际为 ASCII 标点。相同用例在未改动的审计基线 `6614981` 快照同样失败，故本 P0
没有修改检索行为或测试断言来制造通过。

另一次产品运行时与资源退休扩大范围为 `25 passed, 6 failed`。同样两组测试在
未加入私有草稿记录器的 `a7498ea` 独立快照上也是相同的 25/6：一个既有输入预算
断言期待 16384 而实际为 6144，另外五个既有测试替身缺少
`profile_reindex_required`。因此没有把它们记成本补丁回归，也没有在 P0 顺手修改。

仓库既有 mypy 全范围实际运行结果为
`161 errors in 12 files (checked 381 source files)`；错误数和文件数与审计基线的
161/12 相同，新增 `private_replay.py` 没有 mypy 错误。因此不能报告 mypy 通过，
也不在 P0 顺手修复既有类型债务。

私有正文泄漏扫描对 24 条去重后的问题、回答、引用和真值原文检查报告、SAFE Manifest 与
replay 源码，`exact_raw_matches=[]`。

没有运行真实 Provider、线上集成或生产验证。没有创建 8289 新镜像或发出四题重放。

## 10. P1 确定性入口

进入任何修复前，需要完成以下 P0 缺口：

1. 补入并读取 01 审计报告和 `90_contract_probes.py`，执行原脚本并把其中每个反例
   对应到真实函数回归。
2. 取得现有授权服务器访问后，先冻结 `docker inspect` 的镜像、端口和挂载，再对
   `/data/universal-rag.sqlite3` 运行只读 Schema 检查；随后按 trace ID 查询历史行。
3. 如历史行仍无新增字段，最多部署一次仅观测候选到 8289。先记录生产 8288→8088
   只读基线；为候选挂载仓库外 0700 私有目录，以三个显式环境变量启用记录器并将
   捕获上限设为 8，再通过 `--raw-drafts` 指向该目录下的固定输出。随后只发 N031、
   N033、F015、F013 一批请求，每题一次、零重试。
4. N031 以
   `(document_version_id, node_id, table_node_id, row, cell, chunk_id, quote_sha256)`
   作为稳定支持身份，从通道输出开始逐级比较，首次不在集合处即为准确断点。
5. N033 以
   `(claim_sha256, quote_sha256s, selected_support_ids, allowed_support_ids)`
   连接 `generation_evidence.admitted_sources` 的 Chunk/Node/表坐标。只有 replay 的
   `raw_reason_code` 和 `validator` 可以写入最终根因；公开映射码不能替代。
6. 用 `extractive_fallback_reason` 证明 accepted=0 后具体失败分支，再决定 P1 是否有
   独立、确定性的修复入口。

当前可以独立修复的是“观测合同缺失”；N031 的具体召回层修复和 N033 的 Claim
所有权/来源组修复都缺少精确历史输入，不能在 P0 继续猜修。
