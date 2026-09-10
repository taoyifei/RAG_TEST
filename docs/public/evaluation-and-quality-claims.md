# 评测与质量声明

## V3-07 冻结数据集

V3-07 使用 18 份运行时确定性生成、互不含私有材料的 DOCX，经正式 Product API 完成
上传、Job、Revision、查询、History 与 Operational Trace 对账。数据集共 88 个 case，
在查看最终结果前冻结为 44 个 tuning 和 44 个 holdout；80 个可回答、8 个不可回答。
规范摘要为：

```text
sha256:8441be34b25467eba4076dd23260c072d0311f491b16e1f95aee96a24699d3dc
```

全数据集切片分母为：定义 12、目的 10、职责 12、责任角色 10、表格 12、流程/列表
12、数量/序号/否定/版本/来源限定 20。tuning 与 holdout 的文档、实体、文本和 paraphrase
group 相互隔离。阈值同时冻结为：answerable accuracy ≥ 0.90、错误拒答率 ≤ 0.10、错误
作答率 = 0、citation source/span precision = 1.00、paraphrase consistency ≥ 0.90、
hard-constraint violation = 0、History/Trace status parity = 1.00。

## 一次性 Holdout 结果

冻结后只运行一次候选 holdout，随后没有按结果调规则或重跑。44 个 case 的结果为：

| 指标 | 结果 |
| --- | ---: |
| 可回答正确 | 40 / 40 |
| 不可回答正确拒答 | 4 / 4 |
| answerable accuracy / paraphrase consistency | 1.000 / 1.000 |
| false refusal / false answer | 0.000 / 0.000 |
| citation source precision / span validity | 1.000 / 1.000 |
| target document recall / Retrieval Recall@10 | 1.000 / 1.000 |
| support-set recall / precision | 0.91875 / 1.000 |
| hard-constraint violations | 0 |
| History/Trace status parity | 1.000 |

holdout 各切片分母为：定义 6、目的 5、职责 6、责任角色 5、表格 6、流程/列表 6、
硬约束 10；各切片答案正确率、citation source precision 和 status parity 均为 1.000。
support-set recall 不是 1.000，因此不能把本结果表述成“所有可能相关来源都被选中”；它
证明的是已发布答案所需事实完整且所选支持均精确。

## 固定身份 B/C 与性能

基线为 `61775e4775d013a8f809e1f2616af376bac7b2a9`。B/C 使用同一原始 DOCX 字节、
Parser、Chunker、Chunk IDs、活动 Revision、回环 Qdrant、硬件和缓存温度。基线
holdout 的 Retrieval Recall@10 为 1.000，但 answerable accuracy 为 0、错误拒答率
0.625、citation source precision 0.500、hard-constraint violations 7；这表明主要损失在
语义、支持集和回答发布层，不是简单的召回缺失。

tuning 性能门使用至少三份独立关闭态 seed 副本并取 P95 中位数：

| Trace | 基线 P95 | 候选 P95 | 候选/基线 | 20% 门 |
| --- | ---: | ---: | ---: | --- |
| DIAGNOSTIC | 114.852 ms | 123.724 ms | 1.077247 | PASS |
| SAFE | 120.951 ms | 123.856 ms | 1.024018 | PASS |

每次 8 请求 singleflight 样本均为 1 leader / 7 followers，远程 Provider 调用数为 0；
缓存 warm 样本命中率为 1.000。候选一次性 holdout 的本机 cold P50/P95 为
108.901/131.452 ms，TTFC P50/P95 为 108.881/131.432 ms。以上是当前本机、回环、
离线语料结果，不是容量规划或生产 SLA。

## 私有已知回归

用户指定私有语料只在忽略的隔离工作区通过正式 Product API 验证，公开记录仅保留匿名
聚合：15/15 已知可答问题命中正确来源、答案与有效 SourceSpan，另有 5/5 不可回答负例
正确拒答，History 与 Operational Trace 终态一致。私有原件、问题、答案、文件名、
Trace、数据库和 hash 均不进入 Git、公开测试、日志、Review ZIP 或 Docker context。

这组结果是“已知回归”，不是独立 holdout，也不是远程模型质量证据。

## 不能据此声称的事项

V3-07 默认 Lane L 使用本地 deterministic embedding、词面/结构检索与本地 renderer，
没有远程 Provider 调用。它不能证明 Jina/Qwen 的语义质量、Reranker 增益、远程 failover、
Token 成本、生产吞吐、公网代理或真实私有 Provider 质量。真实 Lane R 必须在已批准的
数据出网、模型、operation 与累计预算内单列运行；缺少任一授权时保持 `BLOCKED`。

同维度向量不代表兼容；不同 Provider 的 query vector 不能跨用另一个 vector space。
离线功能合并通过也不等于 OS 风险获批、CI/Branch Protection 完成或生产发布就绪。

## 发布规则

- 数值声明必须同时给出数据集摘要、lane、split、样本数、Trace 模式和候选源码身份。
- tuning、holdout、公开合成、私有已知回归和真实 Provider 结果分别报告。
- 没有运行的字段写 `NOT_RUN` 或 `BLOCKED`，不能转换成 PASS 或数值 0。
- 不发布 query、私有正文、文件名、Secret、绝对路径、原始 Provider body、向量或完整 Trace。
- holdout 一旦影响调参，必须版本化新 holdout，或将已观察集合降级为 tuning。
