# V3-00.6 通用 DOCX 入库合同与安全诊断修复

日期：2026-09-08

本阶段已完成源码实现、通用合成回归、默认 Product 闭环、两份本地输入的
离线重放和完整开发门禁。没有调用远程 Embedding、LLM 或 OCR，没有构建或
发布镜像，没有 push、merge，也没有修改 `main`、`Industry` 或活动生产索引。

## 状态

| 状态项 | 结果 | 依据 |
| --- | --- | --- |
| `ROOT_CAUSE_IDENTIFIED` | `PASS` | 精确复现 `Chunk` 模型级 ValidationError，并定位到 atom 来源资格判断 |
| `DOCX_CONTRACT_MATRIX` | `PASS` | 公开合成矩阵及既有 20 个 DOCX 结构夹具通过 |
| `INGESTION_DIAGNOSTICS` | `PASS` | 根路径、字段路径、脱敏、限长、cause、应用帧和阶段终态均有回归 |
| `PRODUCT_INGESTION_E2E` | `PASS` | HTTP 上传、Job、校验、激活、查询、原件、SQLite 回读和冷启动通过 |
| `SCREENSHOT_INCIDENT_REPLAY` | `PASS` | 截图 SHA 对应原件用默认 Product 和实际文件名重放成功 |
| `CURRENT_CHUNKING_PRESERVED` | `PASS` | 正常 DOCX golden 的内容、顺序、三视图和 SourceSpan 未变化 |
| `DEV_STAGE_GATE` | `PASS` | 2595 passed、89 deselected；静态检查全部通过 |
| `BUILD_CONTEXT_GATE` | `NOT_RUN` | 本阶段不修改构建链或镜像；不复用 V3-00.5 的结果冒充本阶段运行 |
| `RELEASE_SECURITY_GATE` | `BLOCKED` | 沿用 V3-00.5 尚未完成人工风险处置的发布边界 |
| `RELEASE_ACCEPTANCE` | `NOT_RUN` | 上游发布安全门未满足，按依赖失败即停 |
| `LIVE_QUALITY_EVIDENCE` | `NOT_RUN` | 无私有数据出网、真实 Provider 或生产授权 |

## 输入、代码与运行身份

- 执行分支：`codex/universal-selective-word-v3`。
- 阶段起点：`03dc0b0`；实现提交：
  `c51c3de0621b012e7cc9e598bc7e0c8276c936a0`。
- 只读远端参照：Universal `5e21031f72440c0400f3e7798d6b68c64492618d`、
  `main` `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`、Industry
  `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。
- 当前 `rag-app` 入口为 `rag_app.cli:main`，默认 `serve` 进入 Product
  Runtime；`main` 和 `Industry` 只读参照中的 `serve` 仍是旧 API/本地静态页。
- Python `3.11.15`、WSL Git `/usr/bin/git`、SQLite FTS5 `3.51.2`，当前源码树
  导入版本 `0.1.0`。
- 默认 DOCX 路径为 `word-document-v1` → `docx-ooxml-v4` →
  `docx-structural-v3`。API 单文件上传上限为 32 MiB；本地两份输入均在上限内。
- 重放使用默认 strict `ParsingPolicy`，默认 `ChunkingPolicy` 的 target/hard
  max/overlap/min-tail 为 `384/512/64/64`，tokenizer 为
  `deterministic-utf8-v1`，primary Provider 模型上限为 32768 tokens。

两份输入字节身份已分别核对，未因名称相近而混用：

| 本地关联 | 字节数 | SHA-256 |
| --- | ---: | --- |
| 新附件样例 | 22,156 | `92017967d556854bbf5df5db7261547d3075454a45d9abafaac1fada25ce9f7e` |
| 截图作业原件 | 5,467,878 | `9412a755288150d42a190dd4c33be87db6dddc1c3790e8acbd9513f82db0875c` |

原件、正文、完整问题、答案和原始 Trace 均未加入 Git。公开测试使用不同的
设备编号、数值、结构和表达，只保留通用合同。

## 根因与最早偏差

修复前，截图 SHA 对应原件在默认 Product 中实际到达 `chunking` 后成为
`failed_terminal`，错误码为 `CHUNKING_FAILED`。底层是 Pydantic 2.13.4 的
`Chunk` 模型级 ValidationError；旧诊断把 `loc=()` 显示为空字符串，只留下
`main.py:263`，而 `ingestion.chunking.finished` 没有成功/失败状态。

脱敏结构检查显示，原件 IR 有 1623 个节点和 2 个解析 issue；其中 3 个 body
paragraph 各含 8 个 U+0020 空格。首个偏差发生在以下链路：

```text
合法 OOXML 保留空格
  → DocumentIR 如实保留 8 个空格
  → node_text_fragments 仅按字符串 truthy 判定，错误接纳纯空白来源
  → atom/render 生成仅含空白的三视图
  → Chunk 严格模型正确拒绝
```

因此没有修改原件、删除 validator、跳过验证或在失败后丢弃坏 chunk。修复点
放在最早错误的来源资格判定：只有至少一个非空白字符才进入正文 atom；对正常
可见文本不做 `strip`、替换或规范化，标点仍是可见来源。报告的可引用分母同步
采用同一资格规则，并新增纯空白节点数和字符数，不用 clamp 伪造覆盖率。

最小公开用例在修复前得到 3 failed、6 passed：两种独立生成方式的纯空白段落
都触发同一 `Chunk` 错误，全空表格行也被错误表示。修复首因后，同一真实原件
又暴露两个相邻、可独立复现的缺陷：重复的 separator-only 表格片段会生成重复
稳定 ID；长结构上下文恰逢边界时会把派生列表标记 `2. ` 拆开。对应公开用例
均先失败，再由局部修复转为通过。

## 最小改动

1. `atoms.py` 增加不规范化原文的可见字符判断，在 atom 入口排除纯空白节点。
2. `reports.py` 对齐覆盖率分母，并在 `ChunkingReport` 增加
   `whitespace_only_citable_node_count/char_count`。
3. `tables.py` 不再为全空多单元格行制造只有分隔符的正文；含标点或值的行仍
   保留真实行列绑定。
4. `splitting.py` 不发布仅含生成分隔符的 segment，并保证
   `DERIVED_NUMBERING` 原子完整；Unicode grapheme、原文跨度和严格前进不变。
5. `chunker.py` 在 `Chunk` 构造失败时补充格式受限的候选 node/chunk ID，保留
   原始 ValidationError 作为 cause；结构分块版本从 `3.0.1` 升为 `3.0.2`。
6. `revision_builder.py` 的 stage started 事件记录实际 parsing/chunking policy、
   Parser/Chunker、tokenizer identity/exact/compatibility 和各向量槽 Provider、
   model、策略/Provider token 上限。
7. stage finished 事件现在恰有一个 `success/failed/cancelled` 终态，失败时带安全
   error code。收尾 Trace 写入失败只附注到原始异常，不覆盖首因。
8. Pydantic 诊断将 `loc=()` 表示为 `$`，结构化字段路径最多 8 段、错误最多
   8 条，并给出总数和截断标记。模型名、错误类型、cause 类型、第三方原点
   basename 和 repo 相对应用帧均经过白名单；不输出原始 `msg/input/ctx`、正文、
   局部变量、控制字符或宿主绝对路径。

## 通用合同矩阵

矩阵由本阶段新增用例和既有公开保护共同组成，不把私有附件放进 fixture：

| 类别 | 覆盖 |
| --- | --- |
| 标题 | 无标题、同名标题、多级标题、长标题、继承 outline/custom style、标题上下文 |
| 列表 | 自动/手工编号、层级与重启、列表内软换行、长列表跨块、正文混排、逻辑编号计数 |
| 表格 | 短/长表、多段与列表单元格、空格/空单元格/仅标点、横纵合并与延续格、连续重复表头、长行、嵌套表、真实行列关系 |
| 文字 | 不同 run 划分同语义、tab/换行/不间断空格、中文英文、组合字符/emoji、否定、单位、标识符、空尾段 |
| 边界 | 当前真实离线 tokenizer 的 hard max `-1/= /+1`、长标题/表头上下文、overlap、派生编号、grapheme、严格前进 |
| 其它结构 | 同图多处、文本框、页眉页脚、脚注/尾注、批注，均按既有支持策略处理 |
| 确定性与回读 | 同输入重复解析/分块、改名稳定性、run 划分语义对照、Chunk JSON 回读、SQLite 回读和 Product 冷启动 |
| 故障 | 非法 SourceSpan、错误 hash、重复 chunk ID、报告统计不一致、缺失引用、跨 section、损坏 ZIP、资源/深度/超时/外链上限均 fail closed |

固定随机种子的有界长文本和 text/list/table 结构继续验证顺序与严格前进。新增
空白复现分别使用最小 OOXML builder 与 `python-docx`，证明缺陷不依赖单一生成
器。未把 `python-docx` 生成物冒充 Microsoft Word、WPS 或 LibreOffice 实测；
两份本地原件的具体生产工具来源也未核实。

## 默认 Product 与两份输入重放

公开 Product E2E 从 HTTP 上传开始，等待持久 Job，经构建、校验和激活后执行
答案查询与 evidence 检查，按 document/version 读取原件并逐字比对；随后关闭
Runtime，从同一 SQLite 数据目录冷启动，再次查询并回读。Chunk JSON 也通过
`model_validate_json` 重新校验。离线 Mock 只证明管线，不证明真实 Dense 或
LLM 质量。

诊断回归另外证明：新 revision 在 chunking 失败时旧 active revision 保持服务；
失败、取消和成功分别只有一个明确阶段终态；终态 Trace 故障不覆盖原始错误。
既有 `test_retryable_build_resumes_same_revision` 继续保护受控重试沿用同一
revision/Job 身份和 attempt 增量，不增加确定性错误的无限自动重试。

实际离线重放结果：

| 输入 | Product 结果 | Chunk/报告 | 原件与终态 |
| --- | --- | --- | --- |
| 新附件样例 | `succeeded` / `activated` | 57 chunks；coverage 1.0；missing 0；duplicate 0；列表 9/9；表格行 13/13 | 回读 SHA 一致；全部 stage terminal 为 success |
| 截图作业原件 | `succeeded` / `activated` | 445 chunks；coverage 1.0；missing 0；duplicate 0；列表 92/92；表格行 160/160；token max 512；纯空白节点 3/24 chars | 回读 SHA 一致；6 个 stage terminal 全部为 success |

## 正常分块保护与版本影响

冻结的正常原生 DOCX 基线只出现 23 处值差异：7 个 chunk 各自的
`chunker_fingerprint`、派生 `index_revision_id/chunk_id`，以及两个值为 0 的
新增报告字段。DocumentIR、Artifact、chunk 数量/顺序/边界、三种文本视图、
SourceSpan、来源覆盖和结构关系完全一致；没有自动更新其它 golden。

本次是有效分块语义变化，因此 `docx-structural-v3` descriptor 升为 `3.0.2`。
版本变化会改变组件和 index fingerprint，即使正常文档内容不变，派生 revision
和 chunk ID 也会变化。部署采用 3.0.2 时，应对使用该 chunker 的目标 KB 走
“构建新 revision → 完整校验 → 原子激活”，不能把 3.0.1 的旧索引改标签冒充
兼容。旧 active revision 在新构建失败时继续服务；优先受控重试此前因纯空白
节点失败的文档。无需重建不使用此 DOCX chunker 的格式、`main`/`Industry`
只读参照或未采用本版本的环境。

服务进程需要正常重启以加载新代码；没有构建新镜像，因而不能声称任何现有
容器或页面已运行该提交。

## KEEP / REPAIR / ADAPT / DEFER / UNVERIFIED

| 决策 | 能力 | 结论 |
| --- | --- | --- |
| KEEP | Core/Ports/Application/Adapters/Composition、严格 `Chunk`/SourceSpan、三视图、权限与原子激活 | 保留，不回退旧 Runtime 或绕过 validator |
| KEEP | 当前结构化表格、列表、图片元数据、邻居与来源逻辑 | 正常 golden 证明内容行为未被顺手重写 |
| REPAIR | 纯空白来源资格、覆盖率分母、separator-only 表格、派生编号边界 | 已在最早可证明位置局部修复 |
| REPAIR | 安全诊断与 stage finished 语义 | 已增加有界定位与明确终态，不把 finished 当成功 |
| ADAPT | 新诊断字段及 3.0.2 指纹 | 使用当前 Trace/错误模型和现有 Product 生命周期适配 |
| DEFER | V3-00.7 语义问答/真实 LLM、V3-01 图文、V3-02 OCR 关系、V3-03 真实流式 | 明确不在本阶段提前实现 |
| UNVERIFIED | 真实远程 Provider 质量、生产容器/活动索引、Word/WPS/LibreOffice 产物穷举 | 无对应授权或工具证据，不作完成声明 |

三分支入口只读核对结果如下：

| 代码身份 | 默认入口 | 本阶段处理 |
| --- | --- | --- |
| 当前分支 `c51c3de` | `rag-app serve` → Product Runtime | 实际修复与完整验证 |
| `main` `af30f81` | `rag-app serve` → 旧 API/静态页 | 只读参照；未 port、merge 或运行其生产入口 |
| `Industry` `5cc5d7b` | `rag-app serve` → 旧 API/静态页 | 只读参照；未整体合入旧 Runtime |

三者均声明 Python 3.11、`rag_app.cli:main` 和 `python-docx==1.2.0`；默认入口
语义不同，因此不能用旧分支测试替代当前 Product 证据。

## 实际验证

| 命令/运行 | 退出状态 | 结果 |
| --- | ---: | --- |
| `.venv/bin/python scripts/dev.py doctor` | 0 | Python、WSL Git、源码导入、SQLite FTS5、临时目录通过 |
| 新用例定向 pytest + V3-00 baseline | 0 | 24 passed、1 个既有弃用 warning |
| Parser/Chunker/Core/入库生命周期相关 pytest | 0 | 121 passed、1 warning |
| `.venv/bin/python scripts/dev.py smoke` | 0 | 72 passed、1 warning |
| `.venv/bin/python scripts/dev.py product-check` | 0 | hardcode audit 通过；74 passed、1 warning |
| `.venv/bin/python scripts/dev.py product-smoke` | 0 | 6 passed、1 warning |
| 截图 SHA 默认 Product 受控本地 harness | 0 | 实际文件名上传到 activated；445 chunks；回读和阶段终态通过 |
| 新附件默认 Product 受控本地 harness | 0 | activated；57 chunks；回读和阶段终态通过 |
| `.venv/bin/python scripts/dev.py check` | 0 | compileall、Ruff、359 个 mypy 源文件、docstring 通过；2595 passed、89 deselected、4 warnings |
| pytest marker collect-only | 0 | 2684 总计；79 local integration、10 live Provider、无重叠；默认离线门禁排除 89 |
| `git diff --check` / staged diff review | 0 | 无 whitespace 错误；未暂存私有输入或阶段外文件 |

完整门禁的 4 个 warning 是 1 个既有 Starlette/httpx 弃用提示，以及测试中的
3 个 Qdrant API key/无法取得模拟 server version 提示。89 个 deselected 来自
明确的 `not local_integration and not live_provider` 离线边界；本阶段新增用例
没有 skip 或 deselect。BUILD、RELEASE、LIVE 均未运行，不能由上述退出码外推。

## 提交与下一阶段入口

实现提交：

- `c51c3de0621b012e7cc9e598bc7e0c8276c936a0`
  `fix(ingestion): 修复 DOCX 空白节点与安全诊断`

本文件将作为独立文档提交；实际文档提交 SHA 以 Git 历史为准。两项均只在
本地分支，没有 push 或 merge。

V3-00.6 到此停止。V3-00.7 只有在用户单独指定后才进入；本阶段通过不代表
真实 LLM、发布安全、release acceptance 或生产部署获准。
