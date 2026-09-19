# WB08R-03G V10：证据优先问答恢复冻结报告

V10 最终为 **FAILED / PAUSED**，merge_allowed=false，production_publish_allowed=false。实现和真实测试确实按“删除、降级或合并冗余运行时门禁，同时重新划分证据、结构闭合、语义复核与发布职责”的方案执行；不是只放宽小门。两份候选均已在专用 8289 服务真实运行，第二轮仍失败，现按约定停止修改并保留详细 P0。

详细断点和恢复条件见 [P0 清单](wb08r-03g-v10-p0-issues.md)。安全版本、测试、部署和清理摘要由同目录 V10 manifest 固化。

## 实施结果

源码保留为两个独立提交：

- a7892fa6cb03ae46fa3c0c4e6f9b8721301e2ad3：撤销词面启发式的一票否决权，移除自由文本 scope 包含判断，统一关系复核输入，保留来源、安全和事实硬边界；复核失败不应抹掉已经接受的事实。
- da854865f4dbb0c8b673b2d23087988d0a8e49cf：尝试把认证表格值闭合为“值 + 行名 + 表头”事实单元，支持多个表头；v4 复核改用服务器登记的紧凑 anchor ID，避免模型复制引文。

本轮保留了必要门禁：ACL、活动版本、真实来源身份、逐字引文、坐标/跨度、主体、阶段、否定、数字和跨来源结构。动作词、短问重叠、词面锚点和 covered_scope 自由文本包含不再承担最终事实证明。

## 只运行了与问答恢复直接相关的验证

没有运行无关全库测试、无关 UI/历史场景或 CI。

- 问答服务、来源合同、关系复核、Provider 和 API 相关范围：338 passed。
- WB08R 候选 runner、真实指标合同、attempt/source/temporal/atom 语义相关范围：152 passed。
- 修改文件 Ruff、5 个修改源码文件 mypy、py_compile、git diff --check：通过。
- A～F：未运行，因为 Preflight-16 失败。

离线通过不等于真实质量通过；本轮恰好证明两者仍有缺口。

## 8289 两轮真实结果

固定数据和运行身份：46 文档、2880 块、索引 irev_0329a35ca9ea700133f7b309160118f0、Qwen/Qwen3-8B-AWQ、输入/输出 6144/1536、rerank 24。两份候选都由相同冻结基镜像离线构建，运行树各 391 文件；冻结 version bundle 前 business_requests=0、provider_calls=0。

| 验收 | C1 a7892fa | C2 da85486 |
|---|---:|---:|
| Preflight 请求 | 16 | 16 |
| 唯一 final | 16/16 | 16/16 |
| 非空且带支持的答案 | 0/16 | 1/16 |
| F015 已知可答样本 | 0/3，全部误拒 | 0/3，全部误拒 |
| repair 调用 | 0 | 0 |
| gate | FAILED | FAILED |

C2 唯一发布答案来自 N035，但真值状态仍是 NEEDS_TRUTH_REVIEW，因此不能计作正确答案。C2 的其余结果为 12 条拒答和 3 条 unknown/BUDGET_BLOCKED。Preflight 的 runner 退出码 0 仅表示完整执行，summary 中 v8_packet_status 明确为 FAILED。

### F015 第一可观察失败

- 主生成 A1 只选 S18 表头，被 CLAIM_TABLE_DEPENDENCY_INCOMPLETE 拒绝。
- 主生成 A2 只选 S13 值，被 CLAIM_QUERY_RELATION_UNDETERMINED 拒绝。
- C2 v4 复核包实际准备了 S1/S13/S19，但三次 HTTP read timeout，约 23.17 秒/次，没有模型结论。
- 服务端新增表格闭合没有把行名和表头投影回 Claim。代码仍用 semantic query_target 精确匹配物理行名，这是本轮首要设计错误。
- accepted=0 后，结构依赖失败没有进入局部 repair；两个 Atom 均未发布。

### N031

目标来源已进入主生成，因此本轮不再归因于召回。三次生成都把一个表格事实拆成 8 条单来源 Claim，全部落在 CLAIM_QUERY_RELATION_UNDETERMINED；关系复核随后因输入预算超限未发送，三次均 BUDGET_BLOCKED。

### 其他样本

- N033：CLAIM_STAGE_UNSUPPORTED。
- F013：CLAIM_MODALITY_MISMATCH。
- F024/F034：CLAIM_SOURCE_MISMATCH。
- A001：复核发生，但 CLAIM_TEXT_UNSUPPORTED 与 CLAIM_QUERY_RELATION_UNDETERMINED 后 accepted=0。

这些硬边界是否存在误拒需要真值证据，不能为通过 Preflight 直接删除。

## 调用和性能

C2 的真实 Provider 计数：embedding.query 16、reranking 16、query.interpret 9、query.rewrite 0、generation 22，repair 0。

| C2 路径 | n | p50 | p95 |
|---|---:|---:|---:|
| 生成并发布 | 1 | 8955.55 ms | 8955.55 ms |
| 拒答 | 12 | 6699.875 ms | 35426.98 ms |
| unknown/BUDGET_BLOCKED | 3 | 23460.9 ms | 23859.17 ms |

没有结构直出或 repair 样本。模型 TTFT 和排队时间未完整观测。拒答 p95 显著受串行复核影响，性能同样不具备发布条件。

## 部署、回滚和清理

- 两份候选仅部署到 60 服务器的 127.0.0.1:8289，候选健康检查均为 200。
- C2 失败后，8289 已回滚 rag-test-wanshitong:wb08r03f-bd00c56，image ID sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c，健康 200。
- 生产容器 ID 7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e、生产 image ID sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306 前后完全一致。60:8288 和 54:18288 最终只读健康检查均为 200。
- 没有修改 Qdrant、索引、业务数据、生产 Compose 或生产镜像。
- 按用户要求，60 与本地的 V10 候选镜像及确认未被容器引用的旧 rag-test-wanshitong 镜像已按精确清单删除，没有执行全局 prune；54/60 临时传输目录也已删除。仍被生产、8289 基线或既有停止容器引用的镜像保留。删除镜像只能通过重建或重新获取恢复。
- 原有 10 个未跟踪评测结果保持未修改、未提交。

私有原始证据保存在 C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03g-v10-20260919/v10-evidence-da85486.private.tar.gz，不把业务正文、答案正文或凭据提交进 Git。

## 冻结和恢复条件

本轮不创建第三份候选，不进入独立 Verification，不替换生产服务。源码提交保留用于审计，但当前不能部署。

如后续重新开启，必须先完成 P0 清单中的合同级修改：物理表格事实身份与语义 target 分离、服务端先闭合事实、逐 Atom repair 状态隔离、复核超时可降级。对应真实回放先由失败转为通过后，才重新获得一次候选资格。
