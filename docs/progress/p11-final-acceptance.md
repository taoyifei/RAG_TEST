# P11 最终验收与有条件合并

唯一总体状态为 [当前验收](../../release/p11-repair-acceptance.md) 及同名 JSON。
本轮只修预算 CLI 接线、README/Legacy 与证据引用，不是 R6，不重做 R1—R5。

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

当前暂停等待用户在本地页面配置原百炼连接，只保存、不测试。预算、风险、Live 和
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
