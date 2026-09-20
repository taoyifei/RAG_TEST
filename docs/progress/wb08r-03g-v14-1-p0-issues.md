# WB08R-03G V14.1：严格候选后的 P0 清单

V14.1 状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`independent_verification_ready=false`。本文件只记录严格 V14.1 实现后的新问题、已观测
证据、未观测边界和恢复门槛；没有实施修复。

## P0-01：后置字段解析请求被当前 Provider 以 HTTP 400 拒绝

P0 ID：`P0_V141_FIELD_RESOLUTION_PROVIDER_HTTP_400`

### 复现问题与目标合同

真实回放问题是：

> 《开发中心三种工作模式》中，需求快验之前必须准备哪些材料？

V14.1 合同要求先在指定文档版本内找到 canonical schema，将口语“准备哪些材料”解析到
真实基础字段；再独立验证 BEFORE/MUST。来源只证明输入项时，应发布安全基础事实，并把
严格限定记为 NOT_ESTABLISHED，内部 `PARTIAL`、对外 `ANSWERABLE`，不能注入“之前必须”。

### 可证实的三段因果链

失败 Trace：`trace_215874f50b0ba5f4fad388faedff637d`。

#### 1. 检索段没有复现 V14 的旧归零

- 来源：`PRE_ANALYSIS / DOCUMENT_AUTHORITY / RESOLVED`。
- 允许文档版本：1；source catalog 完整。
- scope digest：
  `sha256:93976bf6e6104a2eb1c7c727a7ba614f4cf78c0ddb826014fbbe0d864005a1db`。
- 文档结构入口：`entry_available=true`、`complete=true`、`item_count=22`。
- structural：store 0、filter 后 0。
- lexical：store 8、ACL 后 8、defense 后 8。
- dense：store 22、ACL 后 22、defense 后 22。
- 三个通道 `scope_pushdown_applied=true`、`catalog_complete=true`。
- `ACL_DENIED`、`DOCUMENT_OUT_OF_SCOPE`、`VERSION_MISMATCH`、
  `MISSING_SOURCE_IDENTITY`、`EXCLUDED_DOCUMENT` 均为 0。
- generation evidence 25，A1=25，`pre_generation_availability=STRUCTURED_COMPLETE`。

因此这次不能再归因于“全库 top-k 截断后范围内为零”、文档结构不可用、ACL 丢失、版本
不匹配或来源不完整。

#### 2. 绑定段到达了任务书规定的唯一语义解析点

- canonical schema 产生 4 个 `FieldCandidate`。
- `discarded_incomplete_schema_candidate_count=0`。
- `schema_complete_atom_ids=[A1]`。
- `deferred_from_pre_schema_interpret=true`，说明没有在真实 schema 前先让模型猜字段。
- 只调用一次 `query.interpret`，没有 retry。

Provider 调用观测：

| 字段 | 值 |
|---|---|
| endpoint | `10.242.180.54:18000/v1/chat/completions` |
| model | `Qwen/Qwen3-8B-AWQ` |
| input_count | 2 |
| estimated_tokens | 1377 |
| attempt_count | 1 |
| retry_count | 0 |
| provider elapsed | 32 ms |
| field-resolution latency | 39 ms |
| reason_code | `HTTP_400` |
| status_category | `INPUT_INVALID` |

字段解析层随后记录：

- `reason_code=FIELD_RESOLUTION_PROVIDER_UNAVAILABLE`
- `failure_category=PROVIDER_INPUT_TOO_LARGE`
- A1 回到 `AMBIGUOUS`，候选为 F1～F4。

`PROVIDER_INPUT_TOO_LARGE` 是当前客户端对 HTTP 输入无效的统一映射，不能单凭这个名称
断言实际超出 token 上限；现场估算只有 1377 tokens。

#### 3. 限定和发布段按失败关闭

- AnswerPlan 保留 Q1=BEFORE、Q2=MUST，没有把限定删除。
- `field_resolutions=[]`、`selection_digests=[]`。
- task mode 为 `GROUNDED_GENERATION`，coverage 为 `MISSING`。
- generation=0、relation review=0、published Claim=0。
- `answer_path=NO_ANSWER`、`publication_path=NO_PUBLICATION`。
- 唯一 final 为 `INSUFFICIENT_EVIDENCE`，0 引用。

因此安全边界没有被突破，但任务书要求的基础事实部分回答也没有形成。

### 已知与未知边界

已知：当前 Provider 确实拒绝了后置字段解析的输入；服务端没有 retry，应用按失败关闭，
没有把歧义候选冒充已解析字段。

未知：应用 Trace 没有保存 HTTP 400 响应体；候选日志和可取得的模型服务日志没有暴露
被拒字段。因此以下内容均为 `NOT_OBSERVED`：

- 具体不兼容的 JSON Schema keyword；
- 是否是 `response_format`、消息字段、schema 大小或其它请求字段；
- Provider 返回的原始错误正文；
- 修改哪个字段即可兼容。

不允许把“可能不支持某个 structured-output keyword”写成既定根因，也不能因缺少响应体
就删除结构化约束、自动降级自由文本或增加 retry。

### 为什么是严格 V14.1 后的新 P0

- V14.1 的三个旧责任点已按任务书实现，并通过 144 条直接相关离线测试。
- 真实 Trace 证明来源范围、文档结构、范围内候选、字段候选和一次后置 interpret 均到达。
- 前四题保持了 F024、N031、显式来源和显式列名的预期行为。
- 该 HTTP 400 在任务书要求的真实后置字段消歧调用中首次被观察；它不是继续修改范围下推、
  业务同义词或限定规则能有证据解决的问题。
- 用户明确要求严格实施后若出现新问题，不自行修改。因此现场没有创建第二候选。

### 影响

- 安全：本例没有发布无依据的 BEFORE/MUST，也没有错来源。
- 可答性：字段消歧不可用时，已存在的安全基础事实无法进入冻结选择，形成假拒答。
- 兼容性：当前内网 OpenAI-compatible Provider 与此次 `query.interpret` 输入合同不兼容，
  具体差异尚未观测。
- 门禁：严格五题失败，后续同版本变体、Preflight-16 和 A～F 全部停止。
- 发布：不得替换生产，不得进入 04。

### 后续恢复门槛

只有用户另行授权新批次后才可恢复。第一步应是只读、最小化的协议复现实验，而不是修改
问答语义。至少需要：

1. 保存或取得 Provider 原始 400 错误体，并对密钥、正文和内部路径做脱敏。
2. 以 C5 完整请求为基线，逐字段最小化，确定被拒的精确输入合同；结果必须可重复。
3. 若属于 Provider 已知 schema 子集，兼容器只能做等价转换，不得删除 required/enum、
   放宽字段约束或自动回退自由文本。
4. 对等价转换补离线协议测试和真实一题探针，再重跑同一五题；不能把探针通过当作质量通过。
5. 危险时序题仍必须证明：基础字段可发布、BEFORE/MUST 未被提升、coverage 为 PARTIAL、
   对外只有一个 ANSWERABLE final。
6. F024、N031、显式来源、显式列名必须同版本回归通过；之后才可进入 Preflight-16 和 A～F。

本轮没有执行上述恢复动作。

## runner 权限取证事件

第一次回放先发出一条 F024 请求，随后因普通用户无权读取宿主 Trace SQLite 而中断。
这不是 P0，也不是候选回答失败。已恢复 Trace：
`trace_062b261b68e7ff52cb607e3cdea4b464`；结果为安全拒答。

单独证据文件：`runner-permission-failure-trace.private.json`，SHA-256：
`22b1df86d5f11ca9bd8a9a8ac8f8e61e61e4e5890d2a568e4794a95962253667`。

正式五题的 `retry_count=1` 只表示在这一次已发送请求后重启 runner 批次，不是 Provider
重试。总业务请求为 6，正式质量记录为 5。

## 停止、回滚与证据

- tested source：`543b4e3edc49345b9505725c44b8e1beaec43602`。
- 正式 private replay SHA-256：
  `cba6f48c1b84326a5c534dfb6e789cf76abc7c3969317a3fd2f6d8ec9adf3bbf`。
- safe replay SHA-256：
  `96524e555b17bce5e434ccd7504b87441f1348e8b25e63ff9ea0050ffd69eca2`。
- 私有归档 SHA-256：
  `108314029409120cb4212348cb950c2d077bef69d5b07a32b171d0e2cbc69e11`。
- Preflight-16、A～F：未运行。
- 8289：已回滚 `wb08r03f-bd00c56`，live/ready 均为 200。
- 生产：身份未变，60:8288 与 54:18288 均为 200。
- C5 候选镜像及本地/54/60 临时项：已精确清理；未执行全局 prune。

## 本机分析材料

完整材料位于：

`C:\Users\jerry\Desktop\湾研院\湾事通\WB08R03_V14_1_C5_P0_分析证据_543b4e3`

建议顺序：

1. `README.md`：证据范围、隐私级别和复核命令。
2. `01_trace/derived/analysis-summary.json`：五题对比和危险时序完整因果摘要。
3. `01_trace/derived/cases/UNSAFE_TEMPORAL.full.json`：失败题完整 Trace。
4. `01_trace/authorized-target-document-chunks.private.json`：获准目标文档的 canonical chunks，
   用于人工核对真实 schema 与源关系。
5. `02_runtime/candidate-c5.private.log`：候选日志；可确认 400，但没有原始响应体。
6. `03_source_tested/tree/`：测试时完整源码树，避免后续工作区漂移。
7. `04_diff/v14.1-389ec9e-to-543b4e3.patch`：任务书基线到候选的完整差异。
8. `tools/inspect_v141_c5_evidence.py`：可重复生成派生摘要。
9. `SHA256SUMS.txt`：除自身外的全包完整性清单。

该目录包含内部正文和完整 Trace，不上传 Git，不对外分享。
