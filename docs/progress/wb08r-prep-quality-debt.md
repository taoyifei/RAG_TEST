# WB-08R 准备阶段质量债务

状态：`OPEN / HUMAN_REVIEW_PENDING`

来源：WB08R-03G V14.2 `gate-e-full-96` 的 9 个
`WRONG_SOURCE_ANSWER` 检查，共 7 个唯一 Case。

## 证据边界

- 原始证据包：`WB08R03_V14_2_03完整问题与Trace_6187be6.zip`，SHA-256
  `a325ba5e0da303eff825619939a2134782ad72ab40314154bcf3471ecb92fddc`。
- 9 条请求正文在包内
  `runtime-evidence/all-requests.private.ndjson`；完整 Trace 在
  `runtime-evidence/full-traces/`。正文和 Trace 均不复制进 Git。
- 每条证据以 `source_group + run_id + trace_id` 联合识别；相同 `run_id`
  出现在不同批次时不得覆盖。
- “原始证据已保留”和“此前未写入本地登记表”是两件事。下表已恢复索引，
  但没有执行新的业务请求。
- 实际来源版本取自对应 Trace 的
  `retrieval.claim_publication.published_sources`，不是用期望版本反推。
- V14.2 没有人工 semantic review。`WRONG_SOURCE_ANSWER` 是自动检查结果，
  以下人工判定继续保持“未判断”。

## 9 条原始检查索引

所有记录的 `source_group` 均为 `gate-e-full-96`，公共终态均为
`ANSWERABLE`。

| Case / run_id | trace_id | 期望来源版本 | 实际发布来源版本 | 受限 Trace 路径 | 人工判定 |
| --- | --- | --- | --- | --- | --- |
| `WB08R-F-013` / `formal54/WB08R-F-013` | `trace_9b93871c6581682ffdd090f5f8d930e3` | 《软件开发全流程工作指引》补充：首单交付与规模化交付运维工作指引 / `dver_620e4260044303b449b6e37ae145deca` | 中移湾区（广东）创新研究院技术授权管理办法（V1.0） / `dver_2e2ff6bd7c4bb04b3cff0d3aea36712c` | `full-traces/trace_9b93871c6581682ffdd090f5f8d930e3.json` | 未判断 |
| `WB08R-F-036` / `formal54/WB08R-F-036` | `trace_1899187fc13596f920c1d8de559995b2` | 中移湾区（广东）创新研究院有限公司研发项目管理办法（V3.0） / `dver_e521245f273c01e87820e5da1194908a` | 附件1：中国移动研发项目管理办法（2024版） / `dver_a934ae6153f9e7dc4e10c31b70af5c8a` | `full-traces/trace_1899187fc13596f920c1d8de559995b2.json` | 未判断 |
| `WB08R-N-053` / `natural_complex18/WB08R-N-053` | `trace_9ee4072f6f90be5ecef0703e77fa46d5` | 中国移动粤港澳大湾区创新研究院信息安全管理办法（V1.0） / `dver_49fc044dc618f42979bfda8f300c0cdd` | 产品开发全流程工作规范 / `dver_9252d16e26633bd6b63ee609d4bc130f`；产品开发协同实施方案 V1.0 / `dver_67491302d0823e30f274d2e9a3382d0d` | `full-traces/trace_9ee4072f6f90be5ecef0703e77fa46d5.json` | 未判断 |
| `WB08R-N-058` / `natural_complex18/WB08R-N-058` | `trace_9cf948386ed1a80cd16c4c60ebe4f502` | 《软件开发全流程工作指引》补充：首单交付与规模化交付运维工作指引 / `dver_620e4260044303b449b6e37ae145deca` | 研发工时管理实施指引 V2.0 / `dver_475c52a89c272de3186415ba825d4397` | `full-traces/trace_9cf948386ed1a80cd16c4c60ebe4f502.json` | 未判断 |
| `WB08R-N-059` / `natural_complex18/WB08R-N-059` | `trace_f6515b09966ea8e55f58cef1d9b865f2` | 研究开发支出财务管理办法 V1.0 / `dver_119616decd21f7b7834d1e3210bf035f` | 研发工时管理实施指引 V2.0 / `dver_475c52a89c272de3186415ba825d4397`；研发项目管理办法 V3.0 / `dver_e521245f273c01e87820e5da1194908a` | `full-traces/trace_f6515b09966ea8e55f58cef1d9b865f2.json` | 未判断 |
| `WB08R-N-003` / `latency24/WB08R-N-003` | `trace_e1a96d78db7bcb8069de976c42dcd28f` | 湾区研究院直接采购项目方案决策通过后实施流程 V1.0 / `dver_9b5c2aee54e6e2b967058ac3f1950ea8` | 附件1 湾区研究院采购工作实施指引（V2.0） / `dver_b5ee6719a3c6077d5f16a63e24ab5a06` | `full-traces/trace_e1a96d78db7bcb8069de976c42dcd28f.json` | 未判断 |
| `WB08R-N-005` / `latency24/WB08R-N-005` | `trace_627bad4d3ac199399e6f7f818fd2bc6b` | 附件1：中移湾区（广东）创新研究院有限公司采购管理办法（V3.0） / 期望版本未冻结 | 附件1 湾区研究院采购工作实施指引（V2.0） / `dver_b5ee6719a3c6077d5f16a63e24ab5a06` | `full-traces/trace_627bad4d3ac199399e6f7f818fd2bc6b.json` | 未判断 |
| `WB08R-F-013` / `latency24/WB08R-F-013` | `trace_68c3766b87e38a1c7a1a8b63cd1d854e` | 《软件开发全流程工作指引》补充：首单交付与规模化交付运维工作指引 / `dver_620e4260044303b449b6e37ae145deca` | 中移湾区（广东）创新研究院技术授权管理办法（V1.0） / `dver_2e2ff6bd7c4bb04b3cff0d3aea36712c` | `full-traces/trace_68c3766b87e38a1c7a1a8b63cd1d854e.json` | 未判断 |
| `WB08R-N-059` / `latency24/WB08R-N-059` | `trace_1d15141bd484a435154bbe71c920c9be` | 研究开发支出财务管理办法 V1.0 / `dver_119616decd21f7b7834d1e3210bf035f` | 研发工时管理实施指引 V2.0 / `dver_475c52a89c272de3186415ba825d4397`；研发项目管理办法 V3.0 / `dver_e521245f273c01e87820e5da1194908a` | `full-traces/trace_1d15141bd484a435154bbe71c920c9be.json` | 未判断 |

合计检查次数为 `2 + 1 + 1 + 1 + 2 + 1 + 1 = 9`。这些问题进入阶段
04 / Quality，不因准备阶段工程门禁通过而自动关闭。

## F017 与 N060 的历史拒答证据

下列 6 条 V14.2 记录均为 `INSUFFICIENT_EVIDENCE`。它们证明两题的拒答模式
在本轮 P0 修复前已经存在，但不能替代当前候选 Replay6，也不能证明不同批次的
请求上下文完全相同。

| Case | source_group / run_id | trace_id | 受限 Trace 路径 |
| --- | --- | --- | --- |
| `WB08R-F-017` | `gate-c-failed-24` / `formal54/WB08R-F-017` | `trace_aba502cb41ef5015c83f9d4cd1b1c410` | `full-traces/trace_aba502cb41ef5015c83f9d4cd1b1c410.json` |
| `WB08R-F-017` | `gate-e-full-96` / `formal54/WB08R-F-017` | `trace_26f730a37109914f244e99a562b5eb21` | `full-traces/trace_26f730a37109914f244e99a562b5eb21.json` |
| `WB08R-F-017` | `gate-e-full-96` / `latency24/WB08R-F-017` | `trace_f9b6422b52ab6d9533900a8b91c4ac47` | `full-traces/trace_f9b6422b52ab6d9533900a8b91c4ac47.json` |
| `WB08R-N-060` | `gate-c-failed-24` / `natural_complex18/WB08R-N-060` | `trace_d2e891a2857d7a658c1590c900581a8f` | `full-traces/trace_d2e891a2857d7a658c1590c900581a8f.json` |
| `WB08R-N-060` | `gate-d-natural-36` / `natural_complex18/WB08R-N-060` | `trace_3499cae8760e035264aa8d326393f785` | `full-traces/trace_3499cae8760e035264aa8d326393f785.json` |
| `WB08R-N-060` | `gate-e-full-96` / `natural_complex18/WB08R-N-060` | `trace_615ef69a7ec40a30cc62f928c2480505` | `full-traces/trace_615ef69a7ec40a30cc62f928c2480505.json` |

人工评审完成前，上述 7 个错来源 Case 和 2 个历史拒答 Case 均保持开放；不在
准备阶段修改召回、Prompt、模型或时限来追逐这些普通质量问题。
