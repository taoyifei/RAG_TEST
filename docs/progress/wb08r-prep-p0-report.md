# WB-08R 准备阶段 P0 收尾报告

## 结论

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
