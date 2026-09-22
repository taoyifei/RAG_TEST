# WB08R-03G-P0 冻结证据与第一断点报告

## 1. P0 结论

本微阶段状态为 **PASS_WITH_DECLARED_PACKAGE_INPUT_GAPS**：

- N031 已用历史 Trace 和一次受控 8289 重放重复确认，三条冻结 Gold Chunk 均未进入
  `rerank.input`。这是当前**第一可观察断点**。各召回通道和融合阶段没有保存完整
  Chunk 身份，所以更早的准确断点仍为 `NOT_OBSERVED`，不猜具体通道。
- N033 已取得模型实际 NaturalClaim、Quote、所选 Support、Atom 允许集合及
  SourceSpan 的私有记录。终止发布的原始错误为 `CLAIM_SOURCE_MISMATCH`，validator
  是 `_validate_natural_support_structure`；公开 Trace 将它映射成
  `CLAIM_SUPPORT_NOT_OWNED`。所选 S1/S2 实际属于同一节点、同一表行且首尾连续，
  但没有非空 `evidence_group_id`，稳定表行身份没有被结构 validator 采用。
- N033 `accepted=0`，因此局部 repair 条件没有进入；随后安全摘录 fallback 以
  `NO_SAFE_EXCERPT` 失败，最终 `NO_ANSWER`。
- 本轮只增加兼容观测字段、私有草稿记录和受控 replay；没有改检索排名、证据准入、
  校验阈值、生成 Prompt、预算或回答语义。

仍有两个包输入缺口：附件和仓库中均没有 01 审计报告及
`90_contract_probes.py`。当前 03G-P0 Goal 正文作为 02 阶段计划；旧附件已读取，
但其 03F 执行循环已被当前 Goal 替换。因此本报告完成了 N031/N033 的运行时取证和
仓库真实函数反例，不能声称已逐项复核缺失的 01/90 内容。

WB08R-03 继续保持 `PAUSED`，`merge_allowed=false`。本轮不进入 P1、不运行 04、
不推送、不合并，也不清理镜像。

安全机器清单见 `docs/progress/wb08r-03g-p0-manifest.json`。

## 2. 证据等级与包输入

- **事实**：本轮 Git、Docker inspect、SQLite Schema/固定只读查询、历史 SAFE
  Trace、受控 replay 或真实函数测试直接给出的值。
- **推断**：由多个事实连接得到，但没有对应中间对象的直接记录。
- **NOT_OBSERVED**：运行没有保存该对象，或缺失的包文件无法提供该项。

旧镜像行为没有用当前 HEAD 倒推。历史链只使用
`bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` 的候选、同库历史 Trace 和冻结结果；
当前代码 `65214b9a186008b7b27919483dc389b06f22855f` 只用于一次观测候选。

包输入核对：

| 输入 | 结果 |
| --- | --- |
| 当前 03G-P0 Goal | 已读取，作为本微阶段计划 |
| 旧 `goal-objective.md` | 已读取；其中 03F 循环被当前 Goal 明确替换 |
| 01 审计报告 | `NOT_FOUND` |
| `90_contract_probes.py` | `NOT_FOUND`；未伪造执行结果 |
| 仓库 `AGENTS.md`、`wb08r-state.json`、`wb08r-03-issues.md` | 已读取并按当前范围执行 |

## 3. 冻结身份

### 3.1 Git 与工作区

| 项目 | 冻结值 |
| --- | --- |
| 仓库 / 分支 | `taoyifei/RAG_TEST` / `codex/wb-08r-adaptive-rag` |
| 审计基线 / P0 开始 HEAD | `661498176a2c885f7a109c0af0024ae2ad59b88f` |
| 历史运行代码 | `bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` |
| 观测候选代码 | `65214b9a186008b7b27919483dc389b06f22855f` |
| `bd00c56..6614981` | 仅 `wb08r-03-issues.md`、`wb08r-state.json` |
| 原有未跟踪评测文件 | 10 个，全部保留且未纳入提交 |

`bd00c56` 与审计基线的 `grounded.py` SHA-256 都是
`14617067898ec8cd7e3cc44d81c759616fbe13732678bc6c4268de602975be1f`。

### 3.2 生产与候选

| 项目 | 生产只读基线 | 8289 候选基线 |
| --- | --- | --- |
| 容器 | `wanshitong-app` | `wanshitong-wb08r01-app` |
| 容器 ID | `7f46e6d98eff...` | 恢复后 `065c4b45296f...` |
| 镜像 | `rag-test-wanshitong:604ef63` | `rag-test-wanshitong:wb08r03f-bd00c56` |
| 镜像 ID | `sha256:97826be...` | `sha256:621acc79...` |
| 源修订 | `604ef63be84742efdb340fcc763266e7f457c179` | `bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` |
| Docker 映射 | `0.0.0.0:8288->8088` | `127.0.0.1:8289->8088` |
| 本轮结束健康 | HTTP 200、healthy | HTTP 200、healthy |

生产挂载来源为 `/data/tyf/wanshitong/{data,secrets,logs}`，候选挂载来源为
`/data/tyf/wanshitong-wb08r01/{data,secrets,logs}`；两者 secrets 均只读。
生产容器 ID、镜像、端口、挂载及健康在观测部署前后没有变化。

候选 Compose SHA-256 为
`538e42987b9a00e4858e41be3052e6acbeaf783212c8b060a611bd189c4533d5`，
环境配置文件 SHA-256 为
`ce51dd27f970266cb144ad9f892652e1d477631aed38d88efd6398bd70715f01`。
秘密值没有写入报告、日志、源码或镜像。

### 3.3 Active index、Provider/Profile 与数据

| 项目 | 冻结值 |
| --- | --- |
| Project | `prj_b01e6904546c5000f8a0065a2d713436d` |
| KB | `kb_328756522df5f70ad9a852fdb5fea3b5` |
| Profile | `product-runtime` / `pfr_43e020ad153008e3e67c364f0f3b1d75` |
| Active index | `irev_0329a35ca9ea700133f7b309160118f0`；46 documents / 2880 chunks |
| Index fingerprint | `sha256:37ef53011feccfaf0bb01624c0127f026e028e64af0f89517eaa5e36dabc8714` |
| 历史 Trace serving fingerprint | `sha256:d76e13c9af14ca10cb86361ec476a5966aa16872444877af4e4c6706c55dd884` |
| 当前 Profile serving fingerprint | `sha256:189907352eec14c09849a0bc05f3beaf8d2352a642c07c2b7fe322f502fe728a8` |
| Embedding | `Qwen3-Embedding-0.6B`，1024 维 |
| Reranker | `Qwen3-Reranker-0.6B` |
| Generation | OpenAI-compatible / `Qwen/Qwen3-8B-AWQ` |
| 结构化输出 | `response_format`，thinking disabled |
| Dataset | `wanshitong-v2-20260917` |
| Runner SHA-256 | `beb4e0fe5a70bba18225fa7926101c3d274ec4ee41a5b423e4a33a897babc1d9` |
| Dataset Schema SHA-256 | `67ea379f12a15a8cd56a63b254ba90448a85a6297d95c7c4c558757c9f4ea0d6` |

## 4. 同库只读取证

数据库实际路径为 `/data/universal-rag.sqlite3`，大小 1,202,089,984 字节。本轮先查
`sqlite_master` / `PRAGMA table_info`，再执行固定参数化 SELECT，没有业务更新语句。

实际表结构：

- `query_history`：`trace_id, project_id, knowledge_base_id, owner_id, instance_id,
  process_id, created_at, finished_at, expires_at, status, question_sha256,
  body_saved, ciphertext, nonce, duration_ms, metadata_json`
- `query_trace_events`：`sequence, trace_id, occurred_at, event_name, payload_json`

恢复的历史 Trace：

- N031：`trace_6c780ebd91b5b419110e8a72c993abff`
- N033：`trace_19c87593a5df45fa15b369c2bdbd3bcb`

数据库读取使用 SQLite `mode=ro`。实际 replay 为允许 SQLite 创建共享内存而挂载候选
data 目录，但脚本仍没有 SQL 写入路径；Schema 检查和固定查询均为只读。

## 5. 一次 8289 观测部署与受控 replay

完整 Dockerfile 构建在拉取固定 Node 基础镜像时遇到 Docker Hub DNS 失败，失败发生在
容器替换前。随后从现有 8289 候选镜像构建离线观测 overlay，只替换当前 Git 的
`src/rag_app`、源码 revision 和产品资产 manifest：

| 项目 | 值 |
| --- | --- |
| 观测镜像 | `rag-test-wanshitong:wb08r03g-p0-65214b9` |
| 镜像 ID | `sha256:772ad96f0918112e8accf49593e63582de525cafde2cebf4b12dba2976b5695a` |
| 观测容器 ID | `23b16ae3812a...` |
| 源修订 | `65214b9a186008b7b27919483dc389b06f22855f` |
| 基础修订 | `bd00c56f15aa86cfbe875b1700c72b7a23d9d5da` |
| Product asset manifest | `8c854ce5bf038c77864df2e303bf334273669fee9223879d980238f0a36ee4bd8`；37 文件 / 1,107,812 字节 |
| Python tree SHA-256 | `b3f809da28c9f0c285f1cb9e54dde40e4eb9721c0adc60feab3255358b8de9db`；与 Git archive 一致 |
| Gold / 私有 replay 路径命中 | 0 |

私有目录以 0700 挂载，文件为 0600。两次 preflight 因 UID 权限和 SQLite WAL 打开失败
而在 HTTP 请求前退出，capture 大小保持 0，不计业务请求。修正临时挂载后只执行一批：

- N031、N033、F015、F013 各一次；`request_count=4`，`retry_count=0`。
- 捕获 3 份草稿：F013、N031、N033；F015 走直接摘录，没有模型草稿。
- F013 为 `ANSWERED`，1 个生成、接受和发布 Claim。
- F015 为 `ANSWERED`，`generation_calls=0`，走 `EXTRACTIVE_FALLBACK`，发布 11 个支持。

完成后立即停止观测容器并恢复原 `bd00c56` 候选；8288 生产始终未替换。

## 6. N031 证据链与第一断点

### 6.1 冻结 Gold 支持

N031 配对冻结真值为 `WB08R-F-015`，三条支持来自文档版本
`dver_73e6ecf5f4678f09da2b2284e6099db5` 的同一表第 2 行：

| Gold | Chunk | Node / 表坐标 | Quote SHA-256 |
| --- | --- | --- | --- |
| G01 行名 | `chunk_5d476cdffac74dc3d578f87d851fdb75` | `node_e97185257088b1bb899332012a01fa8e`；`tbl:2/tr:2/tc:0` | `2b8357b4cef4362574eee1bf1b594a5fb7840af7be0f4128092720ce5aebcc6c` |
| G02 输入 | `chunk_39b41e0c274e2873812c8da6c0a37189` | `node_32a284316300e6b50192060821b2e72b`；`tbl:2/tr:2/tc:2` | `54dc6932a131875dc5709ded1a2a5addec5c7579ea1ecb1165f366a1df39b7bb` |
| G03 启动条件 | `chunk_a8e60e79651e4ab437adf67ebabe2e37` | `node_f1f17c4fe3d4a6b3f3714545e3d21e16`；`tbl:2/tr:2/tc:4` | `9b41bbcd6f14edf0e863ee5ff801f66a0a7adc12a48e468c9514129bb6cba81c` |

这些身份证明固定索引中有对应原文节点，不证明任何召回通道返回了它们。

### 6.2 历史与 live 阶段链

| 阶段 | 历史 `bd00c56` | 本次观测 | 结论 |
| --- | --- | --- | --- |
| 解析 / 索引 | Gold 有 Document/Node/Chunk/表坐标 | Active index 身份一致 | 支持确实在固定索引 |
| Root / Atom 召回 | structural 24、lexical 48、dense 24；只有计数 | 通道完整 Chunk 身份仍未 Trace | 各通道首个缺失为 `NOT_OBSERVED` |
| Fuse | 35 候选；只留 rank contributions | 35 候选；仍无完整 ID | Gold 是否进入 fuse 为 `NOT_OBSERVED` |
| Rerank input | 24 个 Chunk；三条 Gold 全部缺失 | 24 个 Chunk；三条 Gold 全部缺失 | **第一可观察断点** |
| Pre-pack / assemble | retrieval 39、model evidence 16、answer support 0 | 不能恢复缺失 Gold | 下游无法恢复冻结来源 |
| Source-node closure | 新增 0 | 结果不变 | 未恢复 Gold |
| Corrective | `NO_GROUP_NEIGHBOR`，新增 0，post `PARTIAL` | 结果不变 | 未恢复 Gold |
| Sent-pack | `NOT_OBSERVED` | 私有记录捕获，仓库不含正文 | live 仅用于身份核对 |
| 模型 / 发布 | 生成 1、接受 2、发布 2；S1/S2/S3/S4/S8 | 完全相同 | 本地 Claim 门通过，冻结来源门失败 |

**第一断点**固定为 `GOLD_SUPPORTS_ABSENT_AT_RERANK_INPUT`。更准确的位置只能收窄到
“召回通道输出、融合或 rerank 输入裁剪之间”；没有通道 ID 集合就不能继续归因。

### 6.3 实际替代引文的适用性

本次 live 生成证据包含 9 个 Support，来自 5 个替代 Chunk：

`chunk_164ee...`、`chunk_74c463...`、`chunk_acd577...`、`chunk_7737de...`、
`chunk_687114...`。

发布的 5 个 Quote SHA-256 与历史运行完全一致：

`67d182...`、`2c50f3...`、`082257...`、`8ad95e...`、`2169ac...`。

它们都来自同一替代文件，均不等于三条 Gold Quote。原文核对只能支持替代文件自身的
要求，不能证明冻结模式行的输入与启动条件。因此事实适用性为“主题相关”，冻结来源
Gate 继续失败；本报告没有放宽该 Gate。

## 7. N033 原始证据链与终止原因

### 7.1 召回到生成前

历史和 live 都观察到同意图比较 Chunk
`chunk_2ef52e5f6a5bf335c23f85d7bd8d5e42` 位于 rerank 第 9 位，随后在 assemble
被记为 `INCOMPLETE_EVIDENCE_GROUP`。Source-node closure 增加
`chunk_eff310e5b592ec8cf0c3905039193bea`；corrective 为
`CORRECTION_NOT_TRIGGERED`、新增 0、post `MISSING`。

这是 N033 的第一处可观察检索合同降级。它与终止发布原因分开记录，不能用
`INCOMPLETE_EVIDENCE_GROUP` 代替模型 Claim 的 validator 错误。

live sent-pack 有 17 个 Support；实际 NaturalClaim 和 Quote 正文留在仓库外私有记录。
仓库只保留以下摘要：

- Claim：56 字符，SHA-256
  `13ae9a056310f770dba04db1cc4c16c4a66750611ee6838e23d79289e7cdd1a5`。
- 所选 Support：S1、S2；A1 允许集合为 S1～S17，因此不是 Atom 越界。
- Quote：41 字符和 11 字符；SHA-256 分别为
  `e64325fe297346aa91ed62b156ee4115839c3def08cf29f80d8ccb55d0b682f0`、
  `6395d7e87fdc4dd8adc095cacd34d39beebe5144d93d0ebf58bb944baa83bf89`；
  两者都是各自 Evidence 的完整逐字子串。

### 7.2 S1/S2 来源映射

| 字段 | S1 | S2 |
| --- | --- | --- |
| Chunk | `chunk_f3708e3cc8fea193f40fab195567c0c3` | `chunk_7962011586d949b58d927e9c0349e8b5` |
| Document version | `dver_620e4260044303b449b6e37ae145deca` | 同左 |
| Section | `section_90b969653f1524008343ec530c53e26d` | 同左 |
| Node | `node_e61c0e0e7bcb0037962ef2e40c778711` | 同左 |
| Table logical node | `node_de2b97c928deaf8aadcdbc96962fb97e` | 同左 |
| Table group / row | `group_47e8...` / 1 | 同左 |
| Structural path | `tbl:13/tr:1/tc:1` | 同左 |
| Source offset | 0..41 | 41..52 |
| `evidence_group_id` | `null` | `null` |
| Span | `original_text`、citable、非 repeated | 同左 |

因此 S1/S2 同节点、同表行、首尾连续，也没有跨角色或跨阶段拼接。两项
`answer_support` 均为 `UNSUPPORTED / REQUESTED_RELATION_NOT_SUPPORTED`，没有
supporting span；A1 matrix 为 `MISSING`，缺少 `GROUP_COMPLETE` 与
`GROUP_ANCHORED`。

### 7.3 原始 validator 错误与来源组恢复

原始错误链已经直接观察：

```text
raw_reason_code: CLAIM_SOURCE_MISMATCH
validator_stage: answer.validate
validator: _validate_natural_support_structure
public_reason_code: CLAIM_SUPPORT_NOT_OWNED
```

`_validate_natural_support_structure` 对多个支持只接受同一非空
`evidence_group_id`，或相同的 `TABLE_INTERSECTION` 证书。S1/S2 的稳定表行身份相同，
`_source_groups(S1) == _source_groups(S2)`，但两者 `evidence_group_id=null`，所以前置
结构门拒绝。`_validated_source_group_claims` 能把它们放回同一派生来源组，却会再次
调用同一个结构 validator，仍得到 `CLAIM_SOURCE_MISMATCH`。这解释了为什么“来源组
恢复”没有挽救该 Claim。

### 7.4 repair 与 fallback

N033 生成 1 个 Claim、接受 0、发布 0。局部 repair 的条件是
`omitted and accepted`，所以 `accepted=0` 时 `repair_calls=0`；没有发生 repair 失败。

随后 `_safe_extractive_fallback` 返回 `NO_SAFE_EXCERPT`，最终路径为 `NO_ANSWER`。
这次 live 结果补齐了历史 Trace 中 fallback 原因不可见的缺口。

## 8. 通用合同缺陷与最小反例

### 8.1 raw reason 被公开映射合并

函数：`_natural_rejection_code`。

`CLAIM_SOURCE_MISMATCH` 与 `CLAIM_SUPPORT_OUTSIDE_ATOM` 都公开映射为
`CLAIM_SUPPORT_NOT_OWNED`。N033 证明只看公开码会丢失真实 validator 原因。
兼容 SAFE 诊断现在同时保留 raw/public code、validator、Atom、所选与允许 Support ID，
正文只保留 SHA-256。

### 8.2 同表行稳定身份没有进入结构 validator

函数：`_validate_natural_support_structure`、`_source_groups`。

新增真实函数反例构造两个可引用、同节点、同表行、首尾连续且派生来源组相同的
`EvidenceItem`。`_source_groups` 确认相同来源组，结构 validator 仍抛出
`CLAIM_SOURCE_MISMATCH`。这与 N033 的实际 S1/S2 形状一致。

回归：
`tests/evaluation/test_wb08r03g_contract_probe.py::test_same_row_identity_is_lost_before_source_group_recovery`。

### 8.3 accepted=0 跳过局部 repair

函数：`GroundedAnsweringService._answer_with_plan`。

真实应用链和 N033 live 均确认 `accepted=0`、`repair_calls=0`。P0 只记录该条件，不改
repair 预算或触发语义。

### 8.4 fallback 历史原因不可见

函数：`_safe_extractive_fallback`。

历史 `None` 没有稳定原因；兼容观测字段补齐原因，N033 live 为
`NO_SAFE_EXCERPT`。fallback 行为没有改变。

### 8.5 N031 通道身份未 Trace

历史与 live 只有各通道计数和融合 rank contributions，无法比较每个阶段的稳定支持
身份。这是确认的观测合同缺口，也是 N031 暂时不能进行确定性召回修复的原因。

### 8.6 缺失的 90 探针

`90_contract_probes.py` 未在附件或仓库出现，故未执行。本轮新增的是仓库真实函数回归，
没有把隔离行为冒充缺失的 90 脚本或端到端测试。

## 9. 观测改动与私有产物

观测改动包括：

- `GroundedOutcome` / SAFE Trace 增加
  `claim_rejection_diagnostics` 与 `extractive_fallback_reason`；
- 默认关闭的 `PrivateReplayDraftRecorder`，只在显式目录、确认值和上限同时配置时启用；
- `scripts/wb08r03g_private_replay.py` 固定 8289 与四个 case，先检查 Schema，零重试，
  私有输出 0600；
- replay 在公共响应后有限等待 History 终态以及
  `retrieval.claim_publication`、`retrieval.complete`，避免异步 Trace 尚未落库时生成空
  manifest。首次 live manifest 暴露了这个 race；权威结果来自 post-settle SAFE 摘录。

对外 REST path、请求/响应、History Schema 均未改变；排名、准入、阈值、Prompt、
Provider、repair 次数和预算行为未改变。

私有目录：

`C:\Users\jerry\AppData\Local\Codex\diagnostics\wb08r03g-p0-20260919`

ACL 仅当前用户与 SYSTEM。仓库不含正文。主要文件：

| 文件 | 类型 | SHA-256 / 字节 |
| --- | --- | --- |
| `historical-private-replay.json` | 历史私有证据 | `ec37da2f...` / 45,152 |
| `historical-safe-trace-live.json` | 历史 SAFE Trace | `6f56042f...` / 99,879 |
| `wb08r03g-p0-raw-generation-drafts-65214b9.ndjson` | live 原始模型草稿，私有 | `2caf6cae...` / 175,497 |
| `wb08r03g-p0-private-replay-65214b9.ndjson` | live 私有 replay | `596dacaf...` / 200,362 |
| `wb08r03g-p0-safe-manifest-65214b9.json` | 首次 SAFE manifest；Trace 未 settle | `194a6175...` / 5,178 |
| `wb08r03g-p0-post-settle-safe-65214b9.json` | 权威 SAFE Trace 摘录 | `907102a8...` / 67,088 |
| `wb08r03g-p0-private-summary-safe-65214b9.json` | N033 SAFE 身份摘要 | `bd8be47f...` / 6,033 |

完整哈希和分类在仓库 SAFE manifest 中。

## 10. 已执行验证

观测字段与私有 recorder 的先失败证据、随后通过结果已在前序提交冻结。本次收尾实际
执行：

```text
pytest tests/evaluation/test_wb08r03g_private_replay.py \
       tests/evaluation/test_wb08r03g_contract_probe.py
5 passed

pytest tests/application/test_grounded_claim_validation.py \
       tests/application/answering/test_grounded_claim_v5_quotes.py \
       tests/application/answering/test_natural_grounded_answer.py \
       tests/evaluation/test_wb08r03g_private_replay.py \
       tests/evaluation/test_wb08r03g_contract_probe.py
220 passed

ruff check 本轮 replay/探针源码与测试
All checks passed

python -m py_compile 本轮 replay/探针源码与测试
passed

JSON 解析：wb08r-state.json、wb08r-03g-p0-manifest.json
passed

git diff --check
passed
```

前序 P0 已运行的相关范围为 151 passed 和 64 passed。一次扩大检索范围为
98 passed / 1 个审计基线同样存在的标点失败；产品运行时扩大范围为 25 passed /
6 个前序快照同样存在的预算与测试替身失败。没有修改这些断言制造通过。

全仓 mypy 实际结果为 `161 errors in 12 files (checked 381 source files)`，与审计基线
相同，新增观测模块没有 mypy 错误；因此不报告 mypy 通过。

真实 Provider 证据仅限上述一次四题受控 replay。没有全量跑题，没有重复同一失败，
也没有生产部署。

## 11. 清理与保护结果

- 8288 生产容器始终未动，结束时 HTTP 200、healthy。
- 8289 已恢复 `rag-test-wanshitong:wb08r03f-bd00c56`，结束时 HTTP 200、healthy，
  没有 capture mount 或 capture 环境变量。
- 60 上的私有 capture、build/replay 临时目录，54 中转临时文件和本机 WSL 临时目录均
  按精确路径删除。
- 私有原文只保留在受控本地诊断目录。
- 观测镜像 `rag-test-wanshitong:wb08r03g-p0-65214b9` 保留；当前 Goal 明确禁止 P0
  清理镜像。

## 12. P1 确定性入口

### N031

目前不能独立确定性修复具体召回层。下一实验应让 Trace 按稳定支持身份记录 structural、
lexical、dense、fusion 与 rerank 输入集合，并以
`(document_version_id, node_id, table_node_id, row, cell, chunk_id, quote_sha256)`
逐级比较。首次缺失处才是可修复层；在此之前不得猜修语义召回或增加业务同义词。

### N033

已有独立、确定性的 P1 入口：让结构 validator 与来源组恢复共用同一个稳定表行身份，
同时保留角色、阶段、原文连续、可引用和 Atom 边界。候选保护必须重放本报告中的
S1/S2 失败基线，并确保跨节点、跨表行、跨角色和无逐字 Quote 的反例继续拒绝。

`accepted=0` 的 repair 条件和 `NO_SAFE_EXCERPT` fallback 是另外两个合同决定，不应与
表行身份修复混成一次改动。

缺失的 01 审计报告和 90 探针仍需补入后做包级交叉复核；它们不改变本次 live 已直接
观察的 N033 raw reason，但会影响是否还有未纳入的通用合同缺陷。P0 到此停止。
