# WB08R-03G V9：两份候选失败后的冻结报告

V9 **FAILED / PAUSED**，`merge_allowed=false`。已用完两份功能候选，停止功能修改。C1 原 Preflight-16 为 **1/16** 行为合格，C2 为 **2/16**；两份均没有发布事实答案。C2 合格项仅 F024、F034 的预期拒答，F034 性能仍失败。A001 的预算拒绝不算成功。A～F、04～06、合并和生产部署均未执行。

详细原因、可证伪假设、逐次 trace 与恢复条件见 [P0 问题清单](wb08r-03g-v9-p0-issues.md)。完整命令、版本、逐 Claim SAFE 诊断和证据摘要见 [安全 manifest](wb08r-03g-v9-manifest.json)。旧 V8 FAILED 报告和历史分母保持不变。

## 实际版本与执行次数

审计基线 `6eaf56f486349d582c349685e20b4fcd3c7118ee`；分支 `codex/wb-08r-adaptive-rag`。源码提交按意图保留，不改写历史：

- `9dc301384f9c41b193d68d381bfbf89e9c7a1790`：规范表头和完整阅读单元。
- `bddf54e5e9ca2bfd27bd80163cb6dfca24bc0a27`：当前 Atom 语义、三态关系、一次批量复核、目标集合覆盖；C1 实测版本。
- `2661ecf8e8b5aa4b7b78a7c4c625a49a0f09f22d`：保留已发送规范结构语境、压缩重复输入、既有证书摘录和精确拒绝诊断；C2 实测版本。

| 身份 | C1 | C2 |
|---|---|---|
| `code_sha` | `bddf54e5e9ca2bfd27bd80163cb6dfca24bc0a27` | `2661ecf8e8b5aa4b7b78a7c4c625a49a0f09f22d` |
| `image_id` | `sha256:468abd241c013b8d52de205173fd7cdbd3242caa6d063e7f22e3469c92061ced` | `sha256:9c5bb66ddda56aa83a465bff489186199241136cc88cc528eae93d9dacd8eb9d` |
| `runtime_tree_sha256` | `028294e531c1a2b4af3ce79dd5798c57f072b47d362d362a4123d6bae242c0a3` | `8aa8913d4824ad3288f47a1345e98b2b25e346606c644d39ea08ba34550900ac` |
| `version_bundle_sha256` | `9372654c095fed775444196da42d07194a5534b076b44203a15654ef2c89be3f` | `41ba2183819863832ea511f22b87b15006a3a48b60979e41ad8d00e2039596a8` |
| `review_revision` | `wb08r-relation-review-v1` | `wb08r-relation-review-v2` |

最终 `actual_source_sha=tested_candidate_sha=2661ecf8e8b5aa4b7b78a7c4c625a49a0f09f22d`。`report_sha` 指包含本报告和 P0 清单的后续纯文档提交，在安全 manifest 中独立记录；最终远端 HEAD 还包含状态/manifest 提交。报告提交没有继承“新功能实测成绩”。

构建 2 次，实际功能部署 2 次，安全回滚 2 次，真实质量请求 32 次（各候选原 16 次）。C1 有 1 次部署保护脚本在变更前因错误 Compose 路径失败，实际部署数为 0；经容器标签确认真实路径后修正。另一次只读路径诊断失败未改变服务。两次候选构建均使用已冻结基镜像，不联网、不拉取、不安装依赖；391 个运行文件逐项验证，176 个受保护资产未变，前端和依赖继承基镜像，不声称重新构建。构建、运行树、资产自检三个命令均退出 0。

固定配置：46 文档 / 2880 块，索引 `irev_0329a35ca9ea700133f7b309160118f0`，`Qwen/Qwen3-8B-AWQ`，输入/输出 6144/1536，rerank 24，并发上限 4。Gold、原题、次数、配置和索引 hash 两候选一致。原 runner 与审计基线一致；不宣称与更早 V8 实测时尚未更新的 runner hash 相同。实际 serving fingerprint 两份都是 `sha256:ff858c8bb682e1620db264984c09fc176e307d933699b95e7d4ceb9fc30a0707`；C2 另以内部 pipeline revision `wb08r-unified-request-relation-v9-c2` 隔离回答缓存，review 协议升 v2。

## 行为与断点

| 对象 | 原有/基线证据 | C1 | C2 |
|---|---|---|
| 三条费用正例 | 原精准范围 111 过 / 3 失败 | 3 条保留来源条件及 MISSING 矩阵后通过 | 同样通过 |
| A001、跨角色/阶段/数字/否定/无关列表负例 | 不可放松硬边界 | 离线通过 | 离线通过；真实 A001 仍预算失败 |
| N031 表头和阅读单元 | 历史首次丢失位置未观测 | 11 个必要 stable key 实际 SENT | 同 11 个来源完整发送，三次与 C1 身份和请求体 hash 相同 |
| N031 语义 | 结构完整不证明事实受支持 | 每次 5 条事实全拒绝 | 15 条拒绝均定位 `_validate_natural_entailment:5553` |
| N033 | 旧同 cell 连续 S1/S2 结构门已通，旧草稿阶段门失败 | 新草稿引用另一表的 S4，结构拒绝 | 同结构拒绝，不能当连续片段误拒 |
| F015 | V8 三次事实/来源合格，系统 PARTIAL | 三次零发布，发生退化 | 三次仍零发布；A1 只引表头，A2 复核仍缺必要语境 |
| F013 | 旧草稿结构不兼容，职责集合未证实 | 复核输入 7505，发送前预算阻断 | 输入降至 5018 并实际发送；6 个 supported 结果被 scope 核验拒绝 |
| N035 | 固定旧草稿最终关系未知 | 复核后未发布 | supported 后 `SCOPE_NOT_IN_BOUND_SOURCE`，未发布 |

C2 的新结构上下文回归在归档 C1 上真实失败，修复后通过；但真实 F015 的 review 来源仍只有 S13，真实 F013 仍 S3/S4/S6。修复在合成且明确目标的回归中有效，没有解决这些真实输入。实际 Atom.target 和 scope 具体失配字段未被 SAFE 记录，不能把 UNKNOWN 目标、词形差异或模型幻觉当作已证实根因。

## 离线门禁和固定材料

- C1 原 114 项全通过；最终相关范围 1277 通过（91.01 秒）。C2 保留原 114 个 nodeid 全通过；增加 22 项后完整相关范围 1299 通过（90.98 秒，退出 0）。两次均仅 1 条上游 Starlette 弃用提示。
- C2 34 个 Python 文件的 Ruff、format、py_compile、diff check 全通过；18 个源码文件的 mypy 全通过。公开 QueryRequest、PublicChatRequest、QueryResponse、EvidenceItem 完整 JSON Schema 与基线 hash 相同。History/缓存/重启等产品 API 回归通过。
- C2 首轮全范围为 1297 通过/1 失败：API Fake 仍读取 v1 的 `quote`，v2 改为 `quotes`；修正严格协议断言，原问题、14 天事实、缓存/History/授权/计账断言未改，原范围完整重跑 1299 通过。失败日志保留。
- C2 构建前新增审阅反例：另一个 Atom 通过不应让当前 Atom 标 PARTIAL。修复只要求新证书摘录按当前 Atom 回调验证；原完整根来源约束仍执行。修改前失败和修改后 76 项通过日志保留。
- 两个失败 pytest 在输出完整失败摘要后因残留线程未退出；核对仅本任务 PID 后 SIGTERM，退出 -15，明确保留为失败，不写成通过。最终完整门禁正常退出 0。
- 固定 N033/F013/N035 原草稿通过真实 GroundedAnsweringService 回放，生成器只返回保存草稿、真实 Provider 0 次。N033 旧草稿在 `_validate_bound_scope` 阶段门仍拒；F013 旧草稿结构拒、原句恢复关系未知；N035 旧草稿关系未知。历史缺实际 SENT 包/完整 trusted groups，未补造模型复核结论。
- N031 同索引规范存储只读材料、NeighborExpander、GenerationEvidencePack、PreparedPacket、真实适配器 Fake HTTP 回放保持 11 个来源键。Fake HTTP 1 次，真实 Provider 0 次。固定回放 C2 冻结源码复跑退出 0；一次 `python -c` harness 缺 `__file__` 的失败已保留并纠正。
- C1 归档基线回归有一次 PYTHONPATH 被 pytest 配置覆盖的无效运行，已明确标 `invalid_as_baseline`。有效版本使用 `-o pythonpath=<archive>/src`，真实得到两个失败；未用无效运行证明修改前行为。

精确命令如下；所有命令数组、退出码、原 nodeid 清单、日志 SHA256 在安全 manifest 和私有目录登记。

```bash
.venv/bin/python -m pytest -q tests/application/answering/test_wb08r03g_request_relation.py tests/application/answering/test_grounded_claim_v5_quotes.py tests/application/answering/test_natural_grounded_answer.py tests/application/answering/test_wb08r03g_source_contract.py tests/application/answering/test_structural_source_coverage.py tests/application/answering/test_wb08r03g_temporal_quantities.py
.venv/bin/python -m pytest tests/application/answering tests/application/retrieval tests/adapters/providers/test_generation_budget_units.py tests/adapters/providers/test_prepared_generation_packet.py tests/adapters/providers/test_aliyun_chat_stream.py tests/adapters/providers/test_relation_review.py tests/product/test_grounded_relation_review.py tests/api/test_p09_stream.py tests/api/test_grounded_product.py tests/api/test_query_history.py tests/api/test_operational_trace_product.py tests/adapters/providers/test_relation_review_compact.py -q --tb=short
```

固定回放入口：`fixed-draft-replay.py`（`WB08R_REPLAY_LABEL=c2`）、`n031-table-closure-replay.py`；构建入口：`prepare_v9_candidate.py <SHA> --candidate-number 1|2`；实际门禁入口：`python3 -m evaluation.wanshitong.v2.run_wb08r03g_v8 --gate preflight-16 --private-dir <candidate-dir> --version-bundle <candidate-bundle>`。没有运行单独 90 探针替代门禁，没有运行无关全库门禁或 CI。

## 真实性能与调用

延迟观察线沿用 V8：首 stage p95≤1 秒，Direct/拒答≤8 秒，生成≤15 秒，Planner≤5 秒。首 stage 不是模型 TTFT；TTFT/queue 均 NOT_OBSERVED。p50 为中位数，p95 沿用冻结 nearest-rank，小样本不作分布改善结论。

| 实际结果类别 | C1 n / p50 / p95 ms | C2 n / p50 / p95 ms |
|---|---|---|
| 成功 Direct | 0 / NOT_OBSERVED / NOT_OBSERVED | 0 / NOT_OBSERVED / NOT_OBSERVED |
| 成功生成 | 0 / NOT_OBSERVED / NOT_OBSERVED | 0 / NOT_OBSERVED / NOT_OBSERVED |
| 成功限定/拒答（本轮均拒答） | 1 / 7232.2 / 7232.2 | 2 / 14354.44 / 21637.82 |
| 输入预算失败 | 5 / 9218.15 / 11883.0 | 1 / 11695.16 / 11695.16 |
| 失败生成 | 10 / 16319.8 / 21225.36 | 13 / 16303.53 / 24252.99 |
| repair 请求 | 0 / NOT_OBSERVED / NOT_OBSERVED | 0 / NOT_OBSERVED / NOT_OBSERVED |

| 复核应用端耗时 | C1 n / p50 / p95 ms | C2 n / p50 / p95 ms |
|---|---|---|
| 实际发送的关系复核 | 4 / 7322.837 / 9209.404 | 8 / 10657.255 / 15014.094 |
| 准备阶段预算拒绝 | 5 / 4.469 / 6.648 | 1 / 4.758 / 4.758 |

C1 真实 generation HTTP 20 次＝16 首次＋4 review；9 次 review 准备中 5 次未发送。C2 真实 generation HTTP 24 次＝16 首次＋8 review；9 次准备中 1 次未发送。两份 repair 均 0；每份 embedding 16、rerank 16、interpret 9、rewrite 0。实际 operation 总数分别 61/65；不把准备数重复计入模型调用。actual HTTP attempt 与 Provider 计账逐项核对。

功能成功的生成/Direct 样本均为 0，不可声明成功路径提速。C2 成功拒答 p95=21637.82ms，超过 8 秒观察线；F013 三次失败生成约 24 秒，review 单独约 15 秒。结论 **PERF_FAILED**，不换模型、不增超时、不隐藏复核延迟。私有统计脚本首轮对 v2 误用 v1 过滤导致断言失败，已按 bundle.review_revision 更正，保留并加强 attempt 唯一性、准备数、SENT 数和计账严格相等校验；首失败证据保留。

每份均 16/16 唯一 final、terminal 和 trace，无 cache hit、无历史引用。两次回滚后额外只读回查 History，各有 16 个不同 owner hash，trace/问题/调用/耗时摘要逐项匹配，无会话 context。真实 HTTP History 授权隔离、重启 UI 回读及并发 4 的 A～F 场景未运行，不能据串行 Preflight 声称全部并发验收通过。

## 最终环境、保留与恢复条件

8289 已回滚 `rag-test-wanshitong:wb08r03f-bd00c56`，image `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c`，容器 healthy、HTTP health 200。候选使用独立 data/logs、只读 secrets，未操作 Qdrant、索引或业务数据。两份失败镜像和诊断材料保留，没有清理镜像或卷。

生产容器 `wanshitong-app` 的 ID `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、image `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`、标签 `rag-test-wanshitong:604ef63` 均未改变；healthy，60:8288 health 200，54:18288 首页只读 GET 200。

原 10 个未跟踪文件、15 份 V8 已登记私有证据和旧 V8 报告/manifest 的摘要均匹配。P0/V8 其他文件缺整目录起始快照，保持 NOT_OBSERVED，不编造完整前后比对。私有目录 `C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03g-v9-20260919` 保存原始结果、脚本、日志和回滚收据；密码、原草稿、答案正文和私有业务材料不进入 Git 或镜像。

本次停止后只提交报告/manifest/state 并普通 push，不再改功能。恢复需要用户审阅 P0 后明确新的目标与实验范围；本轮不自动开第三候选、不进入独立 Verification、不合并生产。
