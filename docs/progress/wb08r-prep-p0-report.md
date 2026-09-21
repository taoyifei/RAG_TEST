# WB-08R 准备阶段 P0 恢复与收尾报告

## 当前结论（部署收敛与收尾）

本轮最终状态为 **PREP_READY / QUALITY_STATUS=KNOWN_ISSUES /
NOT_PRODUCTION_RELEASED**。

`WB08R-PREP-P0-01` 所记录的部署复现阻塞已经按恢复方案消除。候选部署描述在
rendered、created、running 三个阶段均无缺失键、语义差异、挂载差异或未分类
差异；8289 候选完成 Replay6 和 Smoke12 的请求、final、Trace 账目。F017、N060
的历史拒答以及 N003、N059 的来源质量问题继续作为质量债，不冒充全绿，也不阻断
进入 04 影子路由。

当前可以进入 **04 影子路由阶段**。本结论不是生产发布授权：60:8288、54:18288
和稳定标签均未改变，后续仍须先在 8289 完成人工试用，并取得用户明确授权，才能
讨论替换生产。

## 当前交付身份

| 对象 | 当前值 | 说明 |
| --- | --- | --- |
| 候选应用镜像 | `rag-test-wanshitong:wb08r-prep-f97aca5a6548` | image ID `sha256:1c0a92e3a61e743df4cd7886170d769a575125cdd814151a7ef3578474326f79` |
| 候选应用源码 | `f97aca5a65481eada07ce7631fc98d9414092e55` | 后续提交只修部署守卫/等待逻辑及本报告，没有改应用运行源码 |
| 部署与运行守卫 | `4f682eed4366b0c3d7b6756e3fd6a8d0b09ea225` | 修正健康等待；当前容器没有为该修正重启 |
| 产品资产 | 37 个文件，1,107,812 bytes | manifest SHA-256 `68a3556944e55cd1b5c4bb30c7ef29166d356d3e0fc778dceb57a5e503fbbfe72` |
| 候选容器 | `wanshitong-prep-candidate-app` | ID `ee9c883ad4c20d36fcfdbad9617df69e76c186dc5efe492b16427987bf15e415` |
| 候选数据根 | `/data/tyf/wanshitong-wb08r-prep-f97aca5a6548` | 与生产挂载源无交集 |

候选容器当前为 running/healthy，`restart_count=0`，live/ready 均为 200。旧 8289
容器 `67e240a2fa0845135b648342a09880ffd7d55abf274b149c7a84ee089b7e778b`
保持 stopped，原镜像、数据和配置均保留为唯一回滚点。

## 配置、数据与启动门禁

- app-only `compose.candidate.yaml` 只管理候选应用，使用已核对的 external 网络，
  不创建、重启或接管 Qdrant；共享 Qdrant 只读使用。
- rendered、created、running 三层守卫均为 `ready=true`；
  `missing_required_keys`、`semantic_mismatches`、`mount_mismatches` 和
  `unclassified_changes` 均为空。
- 旧 8289 实际环境有 52 个有效应用键。服务器历史 Compose 少声明的 14 个键和
  `/private-diagnostics` 挂载已经在版本化候选合同中显式复现；共同键值无差异。
- 主库和 Trace 库通过运行中 SQLite Backup API 克隆，不复制活动 WAL：
  `universal-rag.sqlite3` 为 1,362,415,616 bytes，SHA-256
  `389766bb623da5c4a82eae2c99f1a49b6673be787c7644078d4c5b61819be5ce4`；
  `product-traces.sqlite3` 为 344,653,824 bytes，SHA-256
  `ff999c35f2c38a225ba544601f7e88c83584dd0a92a1967d90ec66e7d103dd965`；
  两库 `quick_check` 均通过。
- 80 个 blob 共 11,588,578 bytes，tree digest
  `18fe6f6c93fbbd5780d3b14ddd28d464e3c046eef74353d41a7de21c06096609c`。
- 以实际运行用户完成 `/data`、`/logs`、`/private-diagnostics` 写入探针并清理探针
  文件；模型设置仍为唯一有效 revision，测试前后安全摘要一致。

首次正式启动时，端点已 ready，但 Docker 首次 health tick 尚未落为 healthy，旧
启动包装脚本因此返回假失败；容器随后原地健康，`restart_count` 始终为 0，旧 8289
也保持停止。该控制面等待缺陷已在 `4f682ee` 修正，没有为修正重启或重发应用请求。

## 真实小集合账目

### Replay6

- 6/6 使用冻结 Case，6/6 恰好一个 final 和一个 terminal；
- 6/6 trace_id 有效且 Trace 可读；重试数均为 0；
- F015、F048、N054、N056 为 `ANSWERABLE`；F017、N060 为历史
  `INSUFFICIENT_EVIDENCE` 质量债；
- F048 命中预期来源，3 个 accepted claim / 3 个 published claim；
- 输出 SHA-256 `91b8595387d9e2f138f6309390620720e812e3ce7937ade909dae7eb9bb5538e4`；
  review SHA-256 `19c8bf4b8f6bf3f72905a2d84c577316e7477ef1ef545b6ee02c80e148711cabb`。

### Smoke12

- 12/12 使用冻结 Case，12/12 恰好一个 final 和一个 terminal；
- 12/12 trace_id 有效且 Trace 可读；`error_code` 均为空，重试数均为 0；
- F024、F034、A001 为安全拒答；F037、F040、F048、N043、N044 为来源合同
  已确认的正常回答；F049、N001 待人工真值复核；
- N003、N059 为已登记的来源质量问题，不是系统/配置失败；
- F048 再次命中预期来源，3 个 accepted claim / 3 个 published claim；
- 输出 SHA-256 `aceb5d41b1e90d9742c3b74a02a64d459e4152085dd5a981ab95814f19466775c`；
  review SHA-256 `3746366ec06efb1fe4411fcc22eb5552696661349625ab5d3227f4a191f55ed7a`。

Replay6 与 Smoke12 没有虚构去重：实际执行 18 次 Case 请求，另有 N043、N044
两次 setup 请求。查询历史由 2,325 增至 2,345，增量 20；Trace 事件由 79,887
增至 80,886，增量 999。

通用自然题 runner 对子集输出 `CASE_SET_MISMATCH`，因为它默认期待完整 Natural36；
这不是本方案冻结 Replay6/Smoke12 的功能失败。F017、N060、N003、N059 的质量
判定按题保留，未把 Trace 合同通过写成答案质量全通过。

## 离线验证、SSO 与生产保护

本轮实际执行的本地相关门禁：

- Python 相关测试：132 passed，1 warning；
- 前端 `WanshitongApp.test.tsx`：22 passed；
- Ruff、Bash 语法和 `git diff --check`：通过。

本机没有安装 ShellCheck，因此未宣称 ShellCheck 通过。没有运行无关全库测试、
Full96、生产业务请求或真实 SSO 联调。

SSO 清单已纠正为 CAS Ticket v1.3，当前为
`REGISTRATION_INPUTS_PENDING`。外部地址、client、secret 安全注入来源和测试账号
尚待提供；按方案不阻断 04，但不能标记 `AUTH_READY`。

生产保护复核结果：

- 60:8288 仍为容器
  `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、镜像
  `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`，
  启动时间仍为 `2026-09-17T01:45:12.001523599Z`，running/healthy，live/ready 200；
- 54:18288 转发进程仍为 PID `3147532`，启动时间仍为
  `2026-09-17 10:08:21 +0800`，目标仍为 60:8288，live/ready 200；
- `wanshitong-stable-20260921` 继续指向
  `746b030d0b63c937d9a17817596c0533f17eb966`，没有移动。

## 当前证据与清理

私有证据目录为：
`/data/tyf/wanshitong-wb08r01/artifacts/wb08r-prep-recovery-e77ed0c-private/`。
目录权限为 0700，37 个证据文件均为 0600；安全 manifest 内容 SHA-256 为
`62574dbd56d37e278330223149580ba4ea1a28cf2271c9237fdb85b795580e2af`。

已删除两轮失败候选镜像及新增层、对应过期 release 目录、60 上的中转归档和临时
守卫文件；本地临时构建目录已删除，本地候选镜像数为 0。没有执行全局 prune，
没有删除 volume。当前候选镜像/目录、旧 8289 回滚镜像/数据和生产镜像均保留。
原十个本地 Trace 文件未删除，整个 Trace 结果目录继续由 `.gitignore` 忽略。

---

以下内容保留首次失败批次的原始 P0 结论和三次修正证据，不能把历史失败改写为
未发生；其中“当前”“最终”等表述均以该历史批次结束时为准。

## 历史 P0 结论（保留）

本轮最终状态为 **P0_RECORDED / NOT_PREP_READY / NOT_PRODUCTION_RELEASED**。

F048 的动态 Atom Schema 与“生成成功但校验未完成”终态归因已完成代码实现和
定向离线验证；但 60 上既有 8289 候选容器不是当前 Compose 管理实例，且服务器
保存的候选 `compose.yaml` 没有声明 14 个既有结构化输出/诊断运行参数。三次有证据
的部署修正后，运行配置预检仍为 `RUNTIME_ENV_PREFLIGHT_FAILED`。按任务约束停止
继续修复，不运行 Smoke12，不替换生产。

P0 编号：`WB08R-PREP-P0-01`

P0 标题：`8289 候选运行配置无法在三次修正内与回滚基线等价复现`

## 已完成代码与离线门禁

| 提交 | 内容 |
| --- | --- |
| `61cb709` | 冻结准备阶段基线、Demo20、Smoke12、Replay6，并忽略整个 Trace 结果目录 |
| `2c7baad` | 校正并冻结 Smoke12、Replay6 选择及问题摘要 |
| `521f3c5` | 动态 Schema 只允许实际发送的 Atom；Schema hash 进入 packet/cache 身份 |
| `1149075` | 区分校验未完成与 Provider 故障；保留公共可重试提示并禁止语义失败负缓存 |

实际通过的定向门禁：

- Provider/Schema 相关 Python：43 passed；
- 终态归因相关 Python：36 passed，另对完整 `test_related_service.py` 执行
  16 passed；
- 前端 `WanshitongApp`：22 passed；
- Ruff、目标 mypy、ESLint、TypeScript 均通过。

前端第一次从仓库根目录调用 Vitest，因未加载前端 jsdom 配置而失败；从
`frontend/` 使用项目 Node 重新执行后 22/22 通过。该次属于命令环境错误，没有
据此修改产品代码。没有执行无关全库测试、Full96 或生产请求。

## 候选构建与三次修正

候选源码为 `114907583ac7a11b655263679ba0c6a17e29b9d4`。断网分层构建曾得到：

- 临时镜像：`rag-test-wanshitong:wb08r-prep-1149075`；
- image ID：`sha256:e9c80c085c535768474d2eb06ae0ed171b0e23d731b0c462501bb229e48f4461`；
- 产品资产：37 个文件、1,107,812 bytes；manifest
  `d0df393a8b50605620f92c14bbd744ab9219bf2736c1e55a4832fec9ec3ff72a7`；
- compatibility manifest SHA-256：
  `9ca7476e0d2649d8193c0ec5c54ab965fa5c47e9e243176a0113eb4d9122867af`。

| 次数 | 证据 | 结果 |
| ---: | --- | --- |
| 1 | Compose 创建前发现旧 8289 容器无 Compose 标签并占用同名；冲突发生在替换前 | 保留旧容器为停止态回滚点后，新候选可启动 |
| 2 | 新旧容器配置哈希比对发现新候选少 14 个结构化输出/诊断变量及 `/private-diagnostics` 挂载 | 使用旧容器环境与挂载重建，但旧版 Compose 未声明这些变量，等价性仍失败 |
| 3 | 生成显式逐键 override，并在重建前比较全部旧环境值、镜像、端口和诊断挂载 | `RUNTIME_ENV_PREFLIGHT_FAILED`；预检停止，没有再次重建 |

到达第三次上限后没有继续试错。

## Replay6 实测边界

在第 2 次修正前的健康候选上实际发送了 6 个冻结请求。该容器后来被证明运行配置
不等价，因此结果只作为 P0 诊断证据，不能作为 PREP_READY 验收。

| Case | 公共终态 | 行为/来源合同 | Trace 合同 |
| --- | --- | --- | --- |
| `WB08R-F-015` | `ANSWERABLE` | 通过 | 通过 |
| `WB08R-F-017` | `INSUFFICIENT_EVIDENCE` | 失败 | 通过 |
| `WB08R-F-048` | `ANSWERABLE` | 通过；期望来源命中 | 通过；3 个 accepted / 3 个 published claim |
| `WB08R-N-054` | `ANSWERABLE` | 通过 | 通过 |
| `WB08R-N-056` | `ANSWERABLE` | 通过 | 通过 |
| `WB08R-N-060` | `INSUFFICIENT_EVIDENCE` | 失败 | 通过 |

runner 首次把 Trace DB 指向 `product-traces.sqlite3`，而 `query_trace_events` 实际在
`universal-rag.sqlite3`。没有重发请求；改用原 trace_id、原答案和原引用进行离线
复核后，6/6 Trace 合同通过。因运行配置门禁失败，Smoke12 为 `NOT_RUN`。

私有证据保留在 60：
`/data/tyf/wanshitong-wb08r01/artifacts/wb08r-prep-1149075-p0/`。

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `replay6-safe.ndjson` | 24,456 | `13e1b6eb01b4d0d0677f2a1df7f64c76f6bae1432dd08c84b2677584c239494b` |
| `replay6-safe-audited.ndjson` | 9,672 | `4cb5fc64d9c83b7031bfedbdd6d01755da2979695710cc4f97ba782042c636be` |
| `replay6-review.private.ndjson` | 16,144 | `a58678aea2517cf2b74456f088285f5b54eb7b4d70f257066f1908f5ac1ef41c` |

## 恢复、生产保护与清理

- 8289 已恢复任务前容器
  `67e240a2fa0845135b648342a09880ffd7d55abf274b149c7a84ee089b7e778b`
  和镜像 `sha256:8da303c05f71e770be10f8845c0d336ad0feeb72839eb5a1baa6ae9de27cb9c1`；
  live/ready 200，Docker health 为 healthy。
- 60:8288 生产仍为容器
  `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`
  和镜像 `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`；
  启动时间保持 `2026-09-17T01:45:12.001523599Z`，live/ready 200。
- 54:18288 live/ready 200；转发进程 PID `3147532`，启动于
  `2026-09-17 10:08:21 +0800`，仍转发至 60:8288。
- 已删除失败候选容器、临时镜像 `wb08r-prep-1149075` 及其新增镜像层、60 构建目录，
  并删除 60、54 和本地的中转归档。没有删除 volume，没有执行全局 prune。
- 60 只保留当前 8289 镜像 `wb08r03g-6187be6`、唯一回滚基线
  `wb08r03f-bd00c56` 和生产镜像 `604ef63`。本地未产生本轮镜像；现有生产镜像与
  回滚基线未删除。
- 原十个本地 Trace 文件未删除，所在目录继续整体忽略。

## 恢复条件

后续如要继续，应先把 60 的 8289 部署配置升级为能声明并预览全部结构化输出参数
和私有诊断挂载的权威版本；在不启动容器的情况下完成与回滚基线逐项等价预检后，
再以全新证据文件重跑 Replay6 和 Smoke12。当前不得替换 18288/8288。
