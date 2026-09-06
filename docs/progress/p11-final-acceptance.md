# P11 最终验收与有条件合并

唯一总体状态为 [当前验收](../../release/p11-repair-acceptance.md) 及同名 JSON。
本轮完成预算 CLI、README/Legacy 与证据接线，并最小修复真实测试暴露的维护比较和百炼响应兼容缺陷。不是 R6，不重做 R1—R5。

## 已授权更新与真实复测（2026-09-06，当前状态）

用户明确回复“允许”后，完成了 app-only 更新和文档 1 次、成功后 query 1 次的真实复测。
运行实例 `rag-v1-app-1` 与已验证候选现已一致：
`sha256:2a726c2b24dbf01fe1069ca19e93c463b67978250a75ab9c87609d8820598b2e`。
App healthy，首页和 /live 均 HTTP 200，没有 RAG_TEST/Mock 环境注入。
Qdrant 容器、启动时间及重启计数未变；连接、Credential、草稿、migration 与账本在更新前后相同。

旧 App 的 d98a8d 镜像已不在 Docker 本地镜像库；初次检查在停机前停止。
先 export/import 保留独立回退镜像并离线验证运行时导入，再停止 App 执行现有 Product 一致性备份、
完整数据卷和独立 Secret 卷归档验证。恢复镜像为
`docx-rag:p11-native-rollback-20260906t071055z`；
备份和更新收据在 `artifacts/p11-final/app-update-20260906T071140Z/`，
根文件系统恢复归档在 `app-update-recovery-20260906T071055Z/`。
没有覆盖旧备份或清理用户卷，没有产品 SQL migration 变化。

两项真实结果：

| 操作 | HTTP / 维度 | estimated / observed | 请求标识 |
| --- | --- | --- | --- |
| embedding.document | 200 / 1024 | 13 / 23 | cda03f57-fa40-9d10-ad17-e4157c88dffd |
| embedding.query | 200 / 1024 | 106 / 56 | 62ce5101-9bb0-9d2a-8300-56ad7845d7cf |

两项均为 `LIVE_VALIDATION_PASSED`，没有重试；用户密钥、Workspace 和 endpoint_mode 没有改变。
原文档步骤累计单次额度已经耗尽，因此通过现有 Session/CSRF 预算修订接口，在同一个 campaign
追加 `aliyun_document_retest_20260906: 1`，没有扩大 25/1000 和每 Provider 600 的累计上限。
在该单次预算作用域内调用原 `ProductAcceptanceBackend._canary`，
核对活动授权、开始/结束身份与真实单次 HTTP 收据，再由原 AcceptanceState 追加 canonical 记录。
没有修改、patch 或注入 Provider、解码器和预算模块，也没有抹去旧失败。
query 使用原 `scripts/release.py acceptance --resume --live --steps config_check,aliyun_query_canary`；
原 evidence_is_current 校验通过后复用文档结果，真实执行 query。
原失败和新成功已从持久 acceptance_steps 中只读核对。

本次授权实际消耗 **2 HTTP / 119 estimated / 79 observed**；
更新前 9 / 196，当前累计 **11 HTTP / 315 estimated**，
known observed=390，旧账 3 次 usage 未知、本地拦截 1 次 / estimated 19 单列。
从本次 P11 最终验收最初的 6 / 157 起算，共新增 5 HTTP / 158 estimated / 148 observed。
两个获准调用均已完成，不继续未授权收费步骤。

实际草稿仍绑定、未激活；原 30 问两路、标签和阈值不变。
现有预算入口按最新账本零调用重算，完整计划仍 `PROPOSED`：
新增 planned=154 / 47653，capped_max=428 / 145546；
提议累计=439 / 145861，Jina 316 / 132142、百炼 123 / 13719。
该保守计划尚未扣除刚通过 canary 的可复用工作，不能据此自动重跑；
后续执行继续按身份/TTL复用有效操作证据，所需累计预算须单独批准。

有效复用修复代码 `b8b2f37bd3e7556fd7088699940300eb099c9a32` 的
1774 项完整检查、[CI 34017539887](https://github.com/taoyifei/RAG_TEST/actions/runs/34017539887)
7/7、同一 2a726c 候选的浏览器/重启/Qdrant/恢复/升级与安全扫描结果；
本轮只改变部署、授权及验收记录，没有源码修改、重建或重复完整测试。
安全仍有 54 个 High/Critical 包版本元组、18 CVE 待有效处置。
Jina 本轮未重测，真实双槽、主路、failover/recovery、原 30 问两路质量未执行。

`CODE_FIXES_READY=true`、`P11_READY=false`、`CI_READY=PASS`；
`FEATURE_MERGE_DONE=false`、`MERGE_TO_MAIN_AUTHORIZED=false`。
Start SHA 保持 7e46cd9c，工作分支 codex/p11-final-acceptance；
release/feature 未合并，无本轮 merge SHA，main/Industry 不变。
当前只剩完整 Live 预算（或明确的下一连续阶段）、OS 风险实际责任人处置和下游真实验收；
不再要求用户改端点、重复批准已完成更新或重复文档/query 测试。

以下是此前诊断、修复和更新前的历史记录；其中待更新/待复测的描述已由本节完成结果取代。

## 百炼响应根因与修复（2026-09-06，修复阶段记录）

**已确认是代码兼容问题，用户 API 有返回。** 用户授权补一次结构诊断后，
真实 HTTP 200 响应包含 `output/request_id/usage`，没有 SDK 外包字段 `status_code/code`。
其中 1 条向量为 1024 维，text_index=0，未经放宽的 ordered_vectors 严格校验通过，usage=23。
请求标识为 `2be215aa-e437-9958-908e-420747346edf`；结构证据在
`artifacts/p11-final/aliyun-response-diagnostic-2.json`。
原代码错误地要求 SDK 字段，因而拒绝有效 HTTP 结果。用户密钥、Workspace、北京端点无需修改。

修复提交 `b8b2f37bd3e7556fd7088699940300eb099c9a32` 已推送至
`codex/p11-final-acceptance`。仅当存在 SDK 状态字段时校验完整状态对；
原生 HTTP 的向量数量、维度、有限数值、非零向量、索引顺序与 usage 校验保持原合同。
显式业务错误和缺一半的 SDK 状态字段仍拒绝。修复前复现为 4 failed / 4 passed；
修复后定向 131 passed。保留了复现、两次静态检查失败及修正记录，
最终完整 `scripts/dev.py check` 为 **1774 passed / 88 deselected / 4 warnings**，
Ruff、mypy（341 源文件）、Google docstrings 通过。
[CI 34017539887](https://github.com/taoyifei/RAG_TEST/actions/runs/34017539887)
精确测试该代码提交，7/7 jobs 成功；报告文档不改变被测业务资产。

新候选为 `sha256:2a726c2b24dbf01fe1069ca19e93c463b67978250a75ab9c87609d8820598b2e`。
原工作区构建因历史 root-owned Trivy cache 无读取权限而在上传上下文前失败；
在同一提交的临时 detached worktree 完成一次构建，临时 worktree 已清理，
没有改动或删除历史 cache。隔离候选浏览器 5 passed / 3 个既有视口互斥 skip、
重启持久性、Qdrant TLS/快照恢复 3 passed、升级 7 passed、
公开 DOCX/受限 DOC/FTS5/真实 loopback TLS 均通过。
doctor、smoke、product-check（74 passed）、product-smoke（6 passed）本次通过。
新镜像安全检查取得完整未过滤 179 项发现，其中 54 个 High/Critical 包版本元组、
18 CVE，无已提供修复版本；Python/NPM 依赖与源代码/镜像 secret scan 通过。
现有风险 overlay 已绑定新扫描，54 个元组与前次完全相同，人工批准字段仍未填写，
`SECURITY_READY=BLOCKED`，不把可修复 0 解释为无风险。

运行实例 `rag-v1-app-1` 仍为
`sha256:d98a8d168e71d3662f0d55aa8c3954c93d28f0ca33650d3d263e631cecabe245`，
healthy，无测试注入；原 Qdrant 未重启。修复新候选尚未更新到该实例，
修复后没有真实 Provider 调用，诊断不能代替 canary 通过。
原文档 canary 失败和采集点错误完整保留；query 按依赖阻断。
Jina 本轮未重测，原有 Jina 历史调用不是本轮全部操作通过的替代证据。
真实双槽、主路、failover/recovery、30 问 primary/standby 质量尚未执行。

本次授权诊断共新增 **3 HTTP / 39 estimated / 69 observed**；
原已用 6 / 157，当前累计 **9 HTTP / 196 estimated**，
known observed=311，旧账 3 次 usage 未知、本地拦截 1 次 / estimated 19 单列。
同一 campaign 原累计 25 / 1000、每 Provider 600 不变；
文档 canary 和两次诊断的单次额度均已用完。
没有未答复的结构诊断请求，也没有继续自动收费调用。

实际草稿 `pfr_4f107e6baec846a9abf31b816fe8c8d8` 已绑定，未激活。
原 30 问两路、标签和阈值不变。按当前账本零调用重算：
新增 planned=154 HTTP / 47653 estimated；
capped_max=428 / 145546（含限定重试）；
提议累计=437 / 145742，Jina 316 / 132142、百炼 121 / 13600。
这是 `PROPOSED`，未批准。审批和执行前还需核对实际方案身份，
并按原失败保留/单次续跑合同处理已耗尽的 canary 步骤额度，不直接复制计划当批准。

当前权威聚合 `CODE_FIXES_READY=true`、`P11_READY=false`、`CI_READY=PASS`。
Start SHA 仍为 `7e46cd9c989b8f00bd740588b3a53408235bf50a`；
release/feature 未合并，无本轮 merge SHA，main/Industry 未改，
`FEATURE_MERGE_DONE=false`、`MERGE_TO_MAIN_AUTHORIZED=false`。
本次执行证据见 `native-*-evidence.json`、`check-aliyun-contract-final.log`
和 `ci-native.json`；有效复用仅保留未变的配置/首绑合同及其原始收据，
不复用旧镜像的扫描或功能检查来证明新候选。

当前需要用户决定的最小集合：
1. 按任务书 5.1，是否允许将上述 App 从 d98a8d 更新至已验证的 2a726c 候选。
   产品 SQL migration 未变；先备份当前产品与 Secret 数据并保留回退镜像，
   仅 App 短暂停机，Qdrant 与数据卷不清理。
2. 按任务书 6，失败即停后的复测需明确追加授权：先文档 1 次，成功才 query 1 次，
   同一 campaign 继续累计；或另行批准实际绑定的完整预算。此前诊断授权已消耗。
3. 剩余 OS 风险由实际责任人按 CVE/源包审阅覆盖全部 54 元组的原 overlay；
   不要求直接接受全部风险，不代填批准人或期限。

以下各节为此前的时间顺序记录，旧“等待诊断/无草稿/未首绑”等描述仅表示当时状态。

## 配置完成后的续跑（2026-09-06）

用户已在原百炼连接选择 `beijing_dashscope`，配置版本为 3，Credential 版本仍为 1。
原 Workspace/Credential 未替换；端点、连接和实际方案的本地诊断通过。
本地页面创建了空的公开合成验收项目和知识库，保存草稿
`pfr_4f107e6baec846a9abf31b816fe8c8d8`，未激活、未上传文档、未建立索引。
草稿使用 Jina v5 主向量、Qwen3.7 备用向量、Jina v3.5 重排及产品默认解析指令与检索策略。
实际预算已绑定该草稿，`actual_profile_bound=true`；完整预算仍为 `PROPOSED`，没有扩大累计授权。

首绑第一次因 Docker inspect 挂载列表顺序不稳定，被误判为维护状态变化，App 已自动恢复。
在同一容器连续 12 次读取中复现：容器状态及挂载字段完全相同，只有列表顺序变化。
`_campaign_container_metadata` 仅规范挂载顺序，保留全部字段比较。
新增回归同时验证挂载来源、权限、名称、容器 ID、镜像和运行状态变化仍被拒绝。
修复提交 `c224f2bc5a5aa0e669e7d7a711d68796aecafb40` 已推送并核对远端。
两次 HTTP 408 推送失败后，确认远端未前进，使用 HTTP/1.1 成功推送；没有强推。

本次定向测试 48 passed；首次完整检查在 Ruff 发现测试 fixture 参数未显式使用，修正后重跑
原完整命令为 **1758 passed / 88 deselected**，Ruff、mypy、Google docstrings 通过。
日志为 `artifacts/p11-final/check-maintenance-fix.log`（保留失败）和
`check-maintenance-fix-rerun.log`。
[CI 34016592580](https://github.com/taoyifei/RAG_TEST/actions/runs/34016592580)
精确测试上述修复提交，7 个 jobs 全成功。Provider、产品镜像和请求语义未修改，未重建本地候选。

原 campaign 已通过既有断网首绑入口导入全部旧账（6 次转发、157 estimated，另有 1 次本地拦截），
App 已恢复、Qdrant 未操作。原累计上限仍为 25 次 / 1000 estimated，每 Provider 600。
首绑收据见 `artifacts/p11-final/campaign-binding-receipt.json`。

百炼文档 canary 实际返回 HTTP 200、供应商 usage=23，却在响应合同校验中失败：
`INVALID_RESPONSE_CONTRACT`，请求标识 `e2c07c49-2e02-975a-8594-50d864ea2af3`。
查询 canary 按依赖阻断，未继续 Jina、双槽、failover/recovery 或 30 问两路质量。
不能据此声称向量可用，也不能认定用户配置错误。

用户随后明确授权排查配置与代码；通过原管理员 Session/CSRF 预算修订入口增加一个
单次诊断步骤，保留同一 campaign、原累计上限、批准文本和全部旧账。
该诊断也返回 HTTP 200、usage=23，但执行者将采集点放在 Adapter，实际连接测试走 Product
校验，导致未采到响应结构。此采集失误已如实保留，不将其计为成功验收。
采集点已修正，并以真实 Product 校验函数执行离线回归：缺少业务状态字段会被拒绝、
带状态字段且向量合法的样例通过；两种样例均能采到结构，HTTP=0。
这只证明一个兼容性疑点，尚不能确认真实响应字段形态。原 canary 失败记录未覆盖。

截至本段，本轮真实调用 2 次 / 26 estimated / 46 observed；累计为 8 次 / 183 estimated，
已知 observed=288，旧账仍有 3 次 usage 未知。新增诊断单次额度已用尽，已请求用户允许
补 1 次结构诊断；在收到明确回复前不再调用。请求正文、密钥、完整向量均未写入诊断证据。
证据位于 `artifacts/p11-final/diagnostic-approval.json`、`aliyun-response-diagnostic.json`
及 `diagnostic-collector-offline.json`。

当前 `CODE_FIXES_READY=true`、`P11_READY=false`、`CI_READY=PASS`；完整预算和镜像风险
尚未批准，release/feature 均未合并，main/Industry 未改。下文为此前基线与操作历史。

## 已授权 App 更新（2026-09-06）

用户明确允许更新并要求更新后暂停。目标 `rag-v1-app-1` 已更新至候选
`sha256:d98a8d168e71d3662f0d55aa8c3954c93d28f0ca33650d3d263e631cecabe245`，
健康状态 healthy，`/live` 与首页均为 HTTP 200。两条连接、Credential 元数据、
Secret 卷文件身份、17 项 migration、调用事件数保持一致；Qdrant 容器 ID、启动时间
与重启次数未变。未发现测试注入，Provider HTTP=0，未首绑 campaign。

原镜像缺失内容层导致 commit 失败，已恢复原 App；随后从容器根文件系统
export/import 保存本地恢复镜像 `docx-rag:p11-rollback-20260906t055842z` 并验证离线加载。
一次备份目录 UID 权限失败也已恢复 App；修正并验证目录写权限后，正式完成备份及更新。
旧失败收据保留，没有覆盖唯一数据。已验证 Product 一致性备份、完整数据卷归档及独立
Secret 卷归档，文件为 0600、目录受限，保存在
`artifacts/p11-final/app-update-20260906T060543Z/backups/`。
实际更新收据在同目录上级 `update-receipt.json`；恢复根文件系统归档在
`artifacts/p11-final/app-update-20260906T055842Z/`。

更新完成时暂停等待用户在本地页面配置原百炼连接，只保存、不测试。预算、风险、Live 和
feature 合并条件仍未满足；下文原实例镜像与待授权更新描述保留为更新前的历史基线。

## 基线与运行目标

- Start / origin/codex/p11-release：`7e46cd9c989b8f00bd740588b3a53408235bf50a`。
- 工作分支：`codex/p11-final-acceptance`，从 fetch 后最新 release 创建；进入时工作区干净。
- feature/universal-rag：`e7d69f14e5ad293b091f6aef98c91f3a3f76e325`，仍是 release 的祖先。
- main：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`；Industry：`5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`，始终只读。
- 原 App `rag-v1-app-1`：`sha256:20864e7e232c03af74e4ef9f7ea48569d40fc2a2821429b423167e99e6c691e1`，正在运行，未停止/更新/重启。该镜像已不在本地镜像库，更新前还需保留原实例可恢复材料。
- 已通过隔离功能门的候选 `docx-rag:v1-candidate`：`sha256:d98a8d168e71d3662f0d55aa8c3954c93d28f0ca33650d3d263e631cecabe245`；镜像源码标签 `ae38b086217f2d186e3c7693df447ce4e580f4a1`。
- 本轮 runtime、frontend、image dependencies、migrations、evaluation 与候选相关资产内容均未变化；预算 helper 仅属于 release CLI。候选 Mock 验收不代表原实例已经更新或 Live 通过。
- 原库已应用 1—17 号 migration，逐一 checksum 与当前候选契约匹配，没有待执行产品 SQL migration。campaign 首绑仍会创建持久账本并导入旧账；尚未执行。
- 原 Qdrant `rag-v1-qdrant-1` 保持运行且未操作；原 App 的受检环境没有 `RAG_TEST_NETWORK=offline`。

## 本次代码与零调用结果

预算入口复用 `--config` 和 `source_profile_revision_id`，由既有 `profile_specs`、
`ResolvedEmbeddingSpec`、`resolve_retrieval_policy` 解析真实草稿。计划记录角色/模型、
query instruct 身份、检索策略、index/serving 指纹、固定 Product V1 解析/分块参数与 dataset hash。
不激活方案、不构造 Product Runtime、不读取/decrypt Credential、不迁移产品库、不访问模型或 Qdrant。

容器 `/data` 通过原命名卷的断网只读辅助容器访问，只挂数据卷；不挂 Secret、不停止 App。
无非空 WAL 的本地读取采用 immutable 并核对读取前后的文件身份；活动 WAL 需要只读挂载，
无法保证时明确 `BUDGET_READONLY_WAL`，不忽略已提交 WAL，也不新建原库的 WAL/SHM。
方案不存在、连接/模型/拓扑或策略非法时失败，不回退默认计划。

实际原库检索方案数量为 **0**，非秘密验收配置也未指定 source_profile_revision_id。
所以 [实际运行生成的计划](../../release/p11-budget-plan.json) 必须保持
`actual_profile_bound=false`、`PROPOSED`、`activated=false`，不能直接批准。
合法草稿绑定路径已有真实离线回归；这不等于原实例已经具备方案。

最新只读旧账为 6 次转发 / 157 estimated，known observed 242，3 次 usage 未知；
本地拦截 1 次 / estimated 19 单列，未知转发 0。本轮 Provider HTTP / estimated 均为 0。
原授权配置仍为累计 25 HTTP / 1000 estimated，每 Provider 600；没有追加授权或新 campaign。

| 默认假设（不可直接批准） | HTTP | estimated input |
| --- | ---: | ---: |
| 已有累计使用 | 6 | 157 |
| 新增 planned | 154 | 47653 |
| 新增 capped_max（含有限重试） | 428 | 145546 |
| 提议累计上限 | 434 | 145703 |

默认每 Provider 累计提议为 Jina 316 / 132142、百炼 118 / 13561。
canary 每项一次；其余 SDK 请求含初次最多 3 次，planned 每 Provider 4 次额外尝试。
固定 30 个问题、primary/standby 两条路径、原阈值和标签未改。
审批前、执行前必须按实际草稿重算并比对身份；变化只使相关计划失效，不清零旧账。

## 复用与实测边界

- 本次业务代码提交：`6c011c342f5ed2f86fd10d64b6ef0fdd0f416243`，已推送工作分支并核对远端 SHA；未发生 release 或 feature 合并。
- 本次 [CI Run 34014470636](https://github.com/taoyifei/RAG_TEST/actions/runs/34014470636) 精确测试上述提交，7 个 jobs 全成功。source SBOM 与完整扫描资产可访问；CI 镜像扫描不替代本地 d98a8d 候选的风险处置。
- 本次定向回归 **57 passed**；原 `scripts/dev.py check` 为 **1751 passed / 88 deselected**，Ruff、mypy、Google docstrings 均通过。日志位于 `artifacts/p11-final/targeted-final.log` 与 `check.log`。
- 有效复用：[合并 CI 33987948399](https://github.com/taoyifei/RAG_TEST/actions/runs/33987948399)，被测 SHA 精确为 Start SHA，7 个 jobs 全成功。创建于 2026-09-05T19:43:05Z，最终更新 19:48:18Z。完整镜像扫描与 source SBOM 资产可访问。旧 Markdown 中 CI BLOCKED 是生成时快照，本轮按既有聚合同步，不用旧 CI 冒充新增 CLI 的 CI。
- 有效复用：R5 原有 smoke、product、前端、正式候选浏览器、重启、Qdrant、备份恢复与升级证据。原已测文件字节未改；新增测试只覆盖预算 CLI，release 函数变化仅在预算入口。逐项保留原身份、证据哈希与本次复用审查。
- 浏览器 3 个 skip 为既有 desktop/mobile 互斥场景，保留解释；不把历史测试数量作为本轮硬编码门槛。
- 本次执行：预算和 release CLI / resolved-policy 定向回归、冻结后完整 check、只读配置/旧账/方案清单、实际零调用计划、当前镜像合同核验与风险补证。
- 未执行：实例更新、campaign 首绑、任何本轮真实 Provider、双槽索引、failover/recovery、30 问两路 Live 质量、feature 合并、远程生产验收。
- 定向测试曾暴露 SQLite sidecar 写入；修复后严格保留原库全部文件集合与字节不变断言。旧失败日志保留，不删除 case 或放宽门禁。

## 安全处置

复用当前候选同摘要的完整未过滤扫描：179 条全等级发现，其中 High/Critical 54 个包版本元组，18 个唯一 CVE；不能称为 54 个独立可利用漏洞。
本次官方基础标签仍指向已钉摘要；Debian 18 个 CVE 的 Trixie 状态全部为 open。
未回退发行版、删除包元数据、移除受限 DOC 或关闭扫描。

本次候选断网补证：Perl x86_64、ptrsize/ivsize 均为 8；Archive::Tar、IO::Compress、
File::GlobMapper、Storable 缺失；systemd-homed/homectl 缺失；antiword/prlimit 保留。
这些条件已追加至 [原风险 overlay](../../release/p11-os-risk-review.json) 中对应条目，
没有将其自动改为 NOT_AFFECTED。54 个元组仍 UNDER_INVESTIGATION，人工批准字段未填。
发行版摘要查询的暂时 EOF 及后续恢复、辅助容器探查实际退出码均保留在原始证据中。

## 需要用户操作的最小集合

| 事项 | 当前缺项及操作 | 授权边界 |
| --- | --- | --- |
| 目标实例更新 | 明确允许将 rag-v1-app-1 更新到上述 d98a8d 候选。执行前备份产品数据与独立 Secret 材料并保留旧实例恢复路径；只有 App 短暂停机，没有待执行产品 SQL migration | 不停止 Qdrant、不清理卷、不覆盖唯一数据；更新后核查无测试注入 |
| 原百炼连接与方案 | 在本地页面编辑原连接，保留 Workspace ID/Credential，明确选择 endpoint_mode；workspace_host 填控制台实际 API Host。保存但不测试。创建 Jina 主向量、Qwen 备用向量、Jina v3.5 重排的方案草稿并保留实际 instruct/检索参数；不激活、不上传私有材料 | 不在聊天/命令行/Git 提供 API Key；不猜 Host 或代选北京模式 |
| 累计预算 | 草稿就绪后由执行者生成真实绑定预算，再分别批准累计上限或较小连续阶段。当前默认计划不可批准 | 不改原 campaign，不自动把任何计划设为 APPROVED；所有重试计账 |
| 剩余风险 | 指定实际责任人审阅已补客观证据，按 18 个 CVE/源包归组决定，原 overlay 最终覆盖全部 54 元组 | 不要求直接接受全部风险；责任人/批准人/环境/期限及人工附件由真实人员提供 |

未满足授权时暂停依赖步骤；不后台循环等待，不自动合入 feature/universal-rag。
既有聚合器本次输出 `CODE_FIXES_READY=true`、`P11_READY=false`、`CI_READY=PASS`、
`SECURITY_READY=BLOCKED`。`FEATURE_MERGE_DONE=false`；`MERGE_TO_MAIN_AUTHORIZED=false`。
