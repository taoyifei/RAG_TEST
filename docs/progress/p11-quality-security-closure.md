# P11 证据、拒答和安全处置收尾

当前结论：工程修复与离线门通过；唯一一轮Live质量 **BLOCKED**，安全 **BLOCKED**，P11未通过。已停止Provider追加调用，保存失败和剩余预算。权威状态为 [JSON](../../release/p11-repair-acceptance.json) / [Markdown](../../release/p11-repair-acceptance.md)。

## 起点、候选与边界

从最新远端 `codex/p11-final-acceptance` 的 `b7e127b4c5c67714ea628092c45f1a546eedd737` 分出 `codex/p11-quality-security-closure`；未丢失原最终验收修复。
代码提交 `60c2501`，测试提交 `b4e18d3`，镜像精简提交 `d1f8d8de04bad3ab1d65ed84943d84cb5c1bc58e`。
实际App候选 `sha256:05d6f9aa4fec7856683ca6a17b949a0ec08a85eab953d58f479f16a6c4bd866f`；2026-09-06 11:40 UTC完成更新，数据/Secret/预算身份一致，Qdrant未重启。冻结代码CI [34029496428](https://github.com/taoyifei/RAG_TEST/actions/runs/34029496428) 7/7 success，精确绑定d1f8d8d。
工作线合并及实际远端结果以最终Git交付收据为准；产品release/feature未推进，main/Industry只读。报告提交不改变候选业务树，不重建镜像。

## 全60条根因与修复

未修改的887276aa实现断网重建60/60原状态和来源数：38正确回答、14正确拒答、2错误拒答、6属性缺失误答。原始channel/rerank分数、完整RankedChunk、逐span原因和Generator正文未持久化；只按真实ID/排序重建，缺分数保持None。人工对照fixture标记unit_synthetic，没有伪造Provider分数。

| 观测证据 | 实现原因 | 修复 | 反例回归 |
| --- | --- | --- | --- |
| 原B先作为A邻块出现后丢失贡献/重排身份 | previous/A/next先插入，seen跳过原B | 所有原候选先建权威ID映射，保留相对重排顺序；新增上下文单列provenance | 互邻、三命中、共享邻块、排列、重复/冲突ID、section/table、scope/version边界 |
| 正例08正确来源两路均rerank第1仍无引用 | 整批pure-dense条件被邻块/FTS否定，整批字面最高阈值剔除段落 | 每个直接候选独立判资格，Dense+FTS仍可语义准入；局部支持与有界span选择 | 混合通道、不相关词法项、角色同义句、rerank bypass资格不冒用 |
| 负例03/05/06有合法相关引用却答非所问 | lexical/exact被当成充分支持 | answer_support纯函数分别判对象、属性/关系、值与单位，Evidence及Confidence消费明确判定 | 缺属性/补属性、错对象、否定/未知、金额面积电话、角色、表格和合法相邻span |
| 原19个来源、missing引用准确记录失败 | 没有指标记录错误 | 保留原标签、SourceRange、样本、阈值与计算 | 三个原合同文件SHA256不变，实际原程序计算各路30条 |

修改范围为neighbors/evidence/confidence/service/search兼容字段、最小answer_support、现有验收收据和重试边界；未新增生成模型、供应商、向量库或前端。所有原合同哈希保存在freeze.json。

离线实际回放通过原全部60条回归，正式Live状态仍为NOT_RUN；网络连接被封死，Provider HTTP=0。定向邻块性质93通过、支持/语义联合80通过、回放66通过、收据联合92通过，存在交叉覆盖，不能相加充当独立样本。完整check覆盖这些测试。

## 唯一正式Live结果

命令：`.venv/bin/python scripts/release.py acceptance --resume --live --steps config_check dual_index primary_query standby_failover recovery citation_quality --container rag-v1-app-1 --config artifacts/p11-final/quality-security-closure/live-config.json --evidence-file artifacts/p11-final/quality-security-closure/combined-evidence.json --report-output artifacts/p11-final/quality-security-closure/live-aggregate.json --exit-scope selected`。

执行11:47:56–11:51:08 UTC，实际子进程exit=2，selected=BLOCKED。原30问×两路均有观测，保存每条before/after、query/rerank尝试ID、真实SourceSpan、Confidence、Generator正文与当前serving身份。两路positive08均ANSWERABLE且引用“个人信息更正申请由隐私专员受理。”，paragraph7、字符0–16；negative03/05/06均拒答，无发布引用。

| 原指标 | primary计划路30条 | standby计划路30条 |
| --- | --- | --- |
| Recall@5 | 1.000000 / n=20 | 1.000000 / n=20 |
| 表格Recall@5 | 1.000000 / n=4 | 1.000000 / n=4 |
| SourceRange覆盖 | 1.000000 / n=20 | 1.000000 / n=20 |
| SourceRange精度 | 1.000000 / n=20 | 1.000000 / n=20 |
| 发布引用有效率 | 1.000000 / n=20 | 0.750000 / n=20 |
| 引用来源精度 | 1.000000 / n=20 | 0.750000 / n=20 |
| 回答状态准确率 | 1.000000 / n=30 | 0.833333 / n=30 |
| 拒答精度 | 1.000000 / n=30 | 0.666667 / n=30 |
| 拒答召回 | 1.000000 / n=30 | 1.000000 / n=30 |
| 拒答F1 | 1.000000 / n=30 | 0.800000 / n=30 |

两路wrong_scope、wrong_revision、wrong_vector_space均0。原四个表格正例保持正确；primary指标30/30，但其中2次query传输失败自然切备用，不能称为30次真实主槽全部通过。全部60条有真实查询尝试，只有44次有效Provider rerank，16次bypass。备用路正例16–20由历史可回答变成本次拒答，是5条实际退化，不能删除或用离线结果覆盖。

本轮Jina传输错误18次（query2、rerank16），无HTTP状态/Provider请求ID，服务端到达及usage未知；不推断为429、TLS或特定Provider根因。备用5题的关系支持与真实span均成立，但纯Dense且rerank不可用，qualified_source_support=0，现有资格边界保守拒答。原程序以MISSING_CASE_BOUND_LIVE_ATTEMPTS_OR_ROUTE阻断，备用原指标6项失败，未创建质量PASS记录。停止追加调用、调参和第二轮Live。

SourceRange指标按原程序计算组装器选中span，拒答时仍可存在，故备用覆盖/精度1.0不等于最终20条有效引用；实际发布仅15/20，citation有效率0.75。该计算边界如实保留，没有把missing引用算合法空引用。原集是已暴露集回归，非独立泛化证明。

## 实际新增与有效复用门

完整check首次2193通过/5失败（中文关键词顺序、跨单元格关键词兼容、未暂存新增源文件的Git门），保留check.log；离线修复后原完整check重跑 **2198通过、88 deselected、4既有警告**，Ruff/mypy/docstring通过。smoke72、product-check74、product-smoke6；前端lint/typecheck通过、单测50、既有浏览器5通过/3视口条件跳过。没有全视觉矩阵重跑。

同一最终镜像完整安全扫描、依赖审计和源码/镜像secret扫描实际执行；隔离容器启动/浏览器/重启持久性通过，Qdrant集成及备份/性能3通过，升级7通过。公开DOCX80个文本视图、FTS5 3.46.1、TLS1.3握手、QdrantClient1.18.0通过；受限DOC仅验证prlimit+antiword对非法DOC受控拒绝，不声称有效DOC全部格式转换。临时服务/卷已清理。

第一次维护预检因旧镜像标签回收与P11资产检查路径不兼容而在停止App前失败；没有部署损坏候选。用原运行容器只读rootfs恢复rollback镜像，330个业务资产文件及OS/Python包逐项一致；修正维护助手后备份三份并验证，再完成唯一实际App更新。回滚镜像、备份和失败收据保留。

| Live步骤 | 状态 | 来源 | 原实际时间 | 原因 |
| --- | --- | --- | --- | --- |
| config_check | PASS | 本次执行 | 2026-09-06T11:47:59.745147+00:00 | LOCAL_CONFIGURATION_VALID |
| aliyun_document_canary | PASS | 有效复用 | 2026-09-06T07:14:31.518485+00:00 | LIVE_VALIDATION_PASSED |
| aliyun_query_canary | PASS | 有效复用 | 2026-09-06T07:14:43.419097+00:00 | LIVE_VALIDATION_PASSED |
| jina_connection | PASS | 有效复用 | 2026-09-06T07:31:47.065904+00:00 | JINA_OPERATIONS_CURRENT |
| dual_index | PASS | 本次执行 | 2026-09-06T11:48:01.131235+00:00 | DUAL_SLOT_VERIFIED |
| primary_query | PASS | 本次执行 | 2026-09-06T11:48:03.542020+00:00 | ROUTE_VERIFIED |
| standby_failover | PASS | 本次执行 | 2026-09-06T11:48:04.847649+00:00 | ROUTE_VERIFIED |
| recovery | PASS | 本次执行 | 2026-09-06T11:48:05.978869+00:00 | ROUTE_VERIFIED |
| citation_quality | BLOCKED | 本次执行 | 2026-09-06T11:51:07.203856+00:00 | MISSING_CASE_BOUND_LIVE_ATTEMPTS_OR_ROUTE |
| final_report | BLOCKED | 本次执行 | 2026-09-06T11:51:07.487741+00:00 | REQUIRED_STEPS_INCOMPLETE |

五项Provider canary有效复用：Provider实现、连接配置、凭据版本、endpoint/payload合同相同且TTL未到期；表中canary收据的new_forwarded_http属于其原执行时间，不计入本轮新增。新runtime使双槽/主路/故障切换/恢复失效，因此本轮实测；22个文档向量缓存逐项语义核对，dual_index新增HTTP=0，没有重新嵌入语料。

## 安全处置和真实待决

最终完整未过滤扫描：Trivy0.74.0，DB2026-09-06 07:00:11 UTC，扫描11:23:21 UTC；所有等级179→173，HC54→50，唯一CVE18。正常purge mount移除4条HC元组，保留87个OS包和50条元组的版本及严重度均未变，无新增；没有删除dpkg/SBOM或ignore-unfixed。发行版未提供当前可用修复；客观豁免0、真实批准0，50条仍UNDER_INVESTIGATION，review_errors=[]。

完整材料：[18个CVE可读摘要](../../artifacts/p11-final/quality-security-closure/security/cve-summary.md)、[原54/54及现50元组对账](../../artifacts/p11-final/quality-security-closure/security/cve-tuple-disposition.json)、[既有风险入口](../../release/p11-os-risk-review.json)。材料覆盖组件、条件、服务入口、安装/加载、官方Debian结论、措施及剩余影响；剩余包括实际加载SQLite FTS5及恶意数据库恢复路径，也包括当前缺组件/架构条件但不符合自动豁免合同的条目。

唯一需要真实人作风险决定的事项是：是否按既有入口接受限定为当前amd64、loopback本地试点、最多14天的剩余风险；owner/approver/approval/expiry仍未批准。不得把non-root、cap-drop或缺少部分调用入口统一当成不受影响。即使风险获准，当前Live质量仍阻断产品集成，本轮不会自动追加复验。

## 同campaign累计预算

| 范围 | 期初HTTP / estimated | 本轮 | 累计 | 剩余 |
| --- | --- | --- | --- | --- |
| 全局 | 226 / 64311 | 128 / 40675 | 354 / 104986 | 85 / 40875 |
| Jina | 177 / 59219 | 95 / 37163 | 272 / 96382 | 44 / 35760 |
| Aliyun | 49 / 5092 | 33 / 3512 | 82 / 8604 | 41 / 5115 |

累计cap仍439/145861，Jina316/132142、Aliyun123/13719，没有新campaign或扩额。citation_quality本輪122HTTP/40424estimated，累计320/396，步骤剩余76HTTP；primary_query与standby_failover步骤剩余0，不能仅看全局余量自动重跑。
本轮known observed30392，累计94902；原3次+本轮18次usage未知，共21。18次传输错误的forwarding也未知，账本保守计入上表。observed与estimated不相加。本地拦截期初193/3169，本轮132/21173，累计325/24342单列；包括故障注入与有限重试阻断，不能全称新增故障注入或真实HTTP。

## 最终标记与收据

`CODE_FIXES_READY=true`、`RETRIEVAL_QUALITY_READY=BLOCKED`、`SECURITY_READY=BLOCKED`、`P11_READY=false`、`FEATURE_MERGE_DONE=false`、`MERGE_TO_MAIN_AUTHORIZED=false`。

所有本轮日志和收据位于 `artifacts/p11-final/quality-security-closure/`；根因root-cause-audit.json、逐题per-case-before-after.json、唯一Live live-result.json、失败分析live-failure-analysis.json、预算budget-final.json、冻结freeze.json。原失败、失败门及回滚材料均保留；本轮未执行生产release、私文出网或独立泛化质量验收。
