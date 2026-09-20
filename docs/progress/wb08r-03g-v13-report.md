# WB08R-03G V13：确定性来源与表格关系收敛报告

V13 最终状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`production_publish_allowed=false`。本轮按《WB08R03 问答阻塞审计与实施方案》
保留 V12 的统一 Wire、部分安全发布和物理表格事实，只收敛明确来源所有权、表格关系
投影与回答完整性。三份候选均只部署到专用 8289；第三轮仍未通过正向通用变体，已按
用户规定的“三次修复即停”停止，不创建第四份候选，不运行后续 A～F，也不替换生产。

详细阻塞、代码证据和恢复门槛见
[V13 P0 清单](wb08r-03g-v13-p0-issues.md)。版本、部署、证据和清理由同目录 V13
manifest 固化。

## 实施内容

本轮源码由三个按功能意图拆分的提交组成：

- `b064b77c32a9b0df47b1daf2f7162f41e4cd36a8`：把显式文档名解析为活动文档身份，
  将来源范围摘要贯穿检索、生成、语义复核和发布；为闭合物理表格事实建立服务端投影，
  来源或关系不完整时保持部分覆盖，不接受模型自行补齐。
- `aba7892cad0b412e942178d99ee5162dc5f92398`：增加表格行列关系、显式来源、极性和
  目标成员完整性的确定性证明；只有问句明确请求且来源结构支持的列关系才能进入发布。
- `69057baeeda69751d1736d40a882a1cf1fe5c802`：将“备齐/备好/准备”类前置材料问法
  规范为来源中的“输入”关系；尝试从关系匹配文本中排除已解析来源标题，支持唯一短行名；
  当 Atom 已有闭合物理事实时，禁止同表 `literal_table_fragment` 作为引用旁路。

V13 没有删除 ACL、活动版本、文档身份、逐字引文、SourceSpan、表格坐标、否定、条件、
义务强度或发布前绑定。第三轮还把 5 个仍读取旧 `evidence` 字段的相关测试迁移到统一
Wire 的 `read_units/allowed_ref_ids`，保留了原预算、来源保留和 repair 隔离断言。

## 只运行了与 03 问答直接相关的验证

没有运行全仓测试、前端、迁移、索引重建或其他功能测试。

- 表格关系、来源范围、Evidence Pack、绑定、投影、目标完整性和物理事实协议：
  `294 passed`。
- Aliyun/OpenAI-compatible 生成、流式、Grounded Wire、自然答案、语义校验、阅读单元
  和预算单元：`71 passed`。
- 修改文件 Ruff、Ruff format、6 个相关源码文件 mypy、独立回答服务导入和
  `git diff --check`：全部通过。
- 一次 pytest 命令因测试文件目录写错而报告 `no tests ran`；更正为实际路径后得到上述
  `294 passed`，该空运行未计入通过数。
- 失败后只运行了 `QueryAnalyzer` 的本地直接诊断，未调用 Provider，也未扩大测试范围。

这些结果证明相关代码合同和回归用例通过，不证明真实问答已达到发布质量。

## 三轮 8289 真实结果

三轮均在任何业务请求前冻结代码、镜像、运行树、配置、活动索引、数据集和 runner；
冻结时 `business_requests=0`、`provider_calls=0`。每轮先执行 F024、N031 各 3 次，
再执行与当前修复直接相关的正反变体。

| 轮次 | 代码 | F024 ×3 | N031 ×3 | 直接相关变体 | 结论 |
|---|---|---|---|---|---|
| C1 | `b064b77` | 3 次安全拒答 | 3 次正确来源答案，但均为 `PARTIAL` | 显式来源预算阻断，其余正向探针未形成完整覆盖 | FAILED |
| C2 | `aba7892` | 3 次安全拒答 | 3 次正确来源答案，但均为 `PARTIAL` | 显式来源仍为 `PARTIAL`；显式列名、危险时序均预算阻断 | FAILED |
| C3 | `69057ba` | 3 次安全拒答 | 3 次 `SUPPORTED`，无“之前/必须”关系注入 | 显式来源仍为 `PARTIAL`；显式列名、危险时序均预算阻断 | FAILED |

### 已确认修通的两个历史 P0

- F024 连续三轮、每轮 3 次均为 `INSUFFICIENT_EVIDENCE`，没有生成调用、回答或引用，
  明确来源边界不再允许其他文档形成不安全答案。
- C3 的 N031 连续 3 次均为 `ANSWERABLE/SUPPORTED`，只引用
  《开发中心三种工作模式》；服务端按“需求快验”行与“输入”列渲染事实，回答没有把
  问句中的口语前置表达升级为“之前/必须”等来源未陈述关系。

这说明确定性文档范围、物理表格事实和口语“备齐”到“输入”的规范化方向有效，应保留。

### 第三轮决定性失败

C3 的显式来源正例“指定文档中的需求快验输入项”引用文档正确，
`relation_gap=false`，Claim 也来自 `physical_table_fact`；但最终覆盖仍为 `PARTIAL`，
并附带未完整回答提示。Trace 显示：

- QueryAnalyzer 将该问法识别为 `DEFINITION`，关系为“定义”，没有形成“需求快验 / 输入 /
  枚举”的强类型语义。
- `target_member_coverage` 要求 3 个成员，只覆盖 1 个，另 2 个被记为
  `TARGET_MEMBERS_NOT_ANSWERED`；这两个成员来自同一行的非请求列。
- 因此真实答案已包含所问输入值、行标签和输入列表头，仍被错误降为部分回答。

另外两个变体在生成前即失败：显式列名问法和危险时序问法都走
`ORIGINAL_FALLBACK`，由自适应 Planner 拆成 2 个 Atom，随后命中
`SEMANTIC_REVIEW_PREFLIGHT_BUDGET_EXCEEDED`。两次均 `generation_called=false`，不是
模型正文失败，也不能通过提高 token 上限或删减安全复核掩盖。

第三轮已达到候选上限，故没有继续运行 New Wire P0～P3、Preflight-16、
EvidencePack-12、CoreAnswer-16、Failed-24、Natural-36、Full-96 或
Concurrency-4。

## C3 固定身份

- code SHA：`69057baeeda69751d1736d40a882a1cf1fe5c802`
- image ID：`sha256:15f1926d2f73d90528c3aa3cc8a1f9fc2f21a80b22289d048c7a5a687c79eaaa`
- runtime tree：`ae4d12b7d5ad31f3ea3233bd1a9e74e4f25e2e4f948f42fd5cded03950e044a3`
- runtime guard：`04b6458151e335a542a8e74c5468aa3f0e461c2df9cabb3b24c673aebdd2c049`
- version bundle：`ccb4005987222f294386b526529e41ae30fc89c463b3232ea11a4b768930277d`
- asset manifest：`e162af4bd6aadb709aecc1d1df10884ac7e441863732e789cef2fb49e8c313e0`
- configuration：`a47e84aa36078162a060e4fffd86b1c513ae4b69ac85d0e4454b9769aa8ca5cf`
- serving fingerprint：`sha256:ff858c8bb682e1620db264984c09fc176e307d933699b95e7d4ceb9fc30a0707`
- 活动索引：`irev_0329a35ca9ea700133f7b309160118f0`
- Query Plan：`wb08r-query-plan-v11`
- Generation Evidence：`wb08r-generation-evidence-v16`
- Source Projection：`wb08r-source-projection-v2`

## 部署、回滚和生产边界

- 三份候选只部署到 60 服务器的 `127.0.0.1:8289`。
- C3 失败后，8289 已回滚到 `rag-test-wanshitong:wb08r03f-bd00c56`；最终容器 ID
  `34af3cd05d35d654f02d3bfdd9987a3ff6b2d8b029fd3f0ee687d04ea92addfe`，image ID
  `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c`，
  `/live` 与 `/ready` 均为 200。
- 生产容器 ID
  `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、image ID
  `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`
  前后完全一致；60:8288 与 54:18288 `/live` 均为 200。
- 未修改 Qdrant、索引、业务数据、生产 Compose、生产容器或生产镜像标签。

## 私有证据与清理

私有回答正文、引用和逐次证据未进入 Git，已保存到：

`C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03-v13-20260920/`

- C1：`wb08r03-v13-c1-evidence.private.tar.gz`，SHA-256
  `1fabe80e0d161ace84ed2a2a63b69d607ba08411850a567547b5bb713589c861`，154460 bytes。
- C2：`wb08r03-v13-c2-evidence.private.tar.gz`，SHA-256
  `eb8b23966363d03b0034596591b95b0fb5cf48bbce1bfb2dfac6cff1ef0dd170`，44367 bytes。
- C3：`wb08r03-v13-c3-evidence.private.tar.gz`，SHA-256
  `9ec4c93758f6c170a8b0ec01920a601e4caf88e7df0d57d2b88684472028427d`，44024 bytes。
- 三份 gzip 完整性检查通过。

60 和本地的三份 V13 候选镜像已按精确 tag 删除；54、60 和本地的传输包、构建上下文、
runner、状态目录和冻结脚本已精确删除。未执行全局 prune。原有 10 个未跟踪评测文件
逐项 SHA-256 与 V9 清单一致，未修改、未提交。

## 冻结结论

V13 保留在分支供审计，但不具备生产部署资格，也不开始 04。下一次恢复必须先把显式
来源解析、表格行列意图和 QueryAtom 建立成同一个强类型合同，再修复 2-Atom 语义复核
预检预算；不得继续追加提示词、提高 token 上限、恢复 repair 或按个别文件名特判。
