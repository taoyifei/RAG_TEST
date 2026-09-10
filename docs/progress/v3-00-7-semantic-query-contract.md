# V3-00.7 自然问法语义、改写与模型调用合同

日期：2026-09-08

本阶段已完成共享查询语义、证据不足后的有界改写、Hybrid 变体贯穿、
结构化列举证据、缓存/本次模型调用事实和前端状态的源码实现，并完成默认
Product、完整离线门禁和最终提交上的真实浏览器验收。没有把问题、品牌、部门名
或答案写入生产逻辑。

真实百炼质量验收仍为 `BLOCKED`：用户已允许使用免费额度，但当前 WSL 环境没有
独立 `DASHSCOPE_API_KEY`，可用凭据仅位于明确禁止访问的生产 Secret 卷；现有
生产 campaign 的 generation 额度也已用完。本阶段没有借用生产卷、停止生产容器、
新建生产 campaign 或发出远程 Provider 请求，不能把 Mock、摘录或浏览器结果写成
真实 LLM 质量通过。

## 状态

| 状态项 | 结果 | 依据 |
| --- | --- | --- |
| `ROOT_CAUSE_DIFF` | `PASS` | 旧运行 Trace 与独立合成复现均证明拒答发生在语义/证据门，不是资料缺失或 LLM 全局未接 |
| `MAIN_INDUSTRY_MATRIX` | `PASS` | 固定 SHA 下对照默认入口、改写、Hybrid、Evidence 和生成拒答；只选择性适配 |
| `SHARED_QUERY_SEMANTICS` | `PASS` | Analyzer、Planner、Rewrite、Lexical、Dense、Rerank、Evidence、Confidence 共用同一类型化语义 |
| `UNSUPPORTED_RELATION_REFUSAL` | `PASS` | 无来源比较题即使有 Dense 候选仍拒答；hardcode audit 通过 |
| `MODEL_CALL_OBSERVABILITY` | `PASS` | HTTP 冷/热缓存、Mock 生成、失败回退及浏览器详情均区分产物来源与本次调用 |
| `PRODUCT_BROWSER_GATE` | `PASS` | 最终实现提交上完成公开合成 DOCX 上传、索引、口语问答、引用和无关比较题拒答 |
| `DEV_STAGE_GATE` | `PASS` | compileall、Ruff lint、Mypy、docstring 及 2649 个默认离线测试通过 |
| `GLOBAL_RUFF_FORMAT_BASELINE` | `BLOCKED` | `ruff format --check .` 发现 219 个跨阶段既有文件；本阶段 22 个 Python 变更文件全部通过 |
| `LIVE_LLM_QUALITY_EVIDENCE` | `BLOCKED` | 有额度授权但没有非生产凭据；生产卷访问禁令仍有效，真实分母为 0 |
| `BUILD_CONTEXT_GATE` | `NOT_RUN` | 本阶段不修改镜像或构建链，也没有构建候选镜像 |
| `RELEASE_SECURITY_GATE` | `BLOCKED` | 沿用 V3-00.5 未完成人工风险处置的发布边界 |
| `RELEASE_ACCEPTANCE` | `NOT_RUN` | 发布安全门未满足，且本阶段没有发布授权 |
| `V3_01` | `NOT_STARTED` | 本轮只执行 V3-00.7，不提前进入 DOC 图文适配 |

## 输入、代码与运行身份

- 执行分支：`codex/universal-selective-word-v3`。
- 阶段起点：`f9c75d5c0e8ffaac4c3ad2709b8d882ce6d0fa5c`。
- 实现提交：`bc1ee969cf1cb67b1a1cb7127f412820a917a1f3`。
- 只读参照：`main` `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`；
  `Industry` `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`；远端 Universal
  `5e21031f72440c0400f3e7798d6b68c64492618d`。
- 当前 Product 入口仍为 `rag-app serve` → Product Runtime；没有切回旧
  `/api/chat` 或另建旁路数据库。
- Python `3.11.15`、WSL Git `/usr/bin/git`、SQLite FTS5 `3.51.2`，源码树
  导入版本 `0.1.0`。
- 只读生产身份核对时，`rag-v1-app-1` 仍运行旧镜像
  `sha256:097a824325be...`、OCI revision `7f4ef25b0567...` 且健康；没有把
  本地实现提交冒充为已部署版本。
- 最终浏览器验收运行干净的实现提交 `bc1ee969...`，使用临时本地 source 服务、
  离线 bootstrap 会话和公开合成 DOCX；服务、标签页和临时文件随后已停止/清理。

## 真实旧链路差分与根因

对同一旧知识库、同一 active revision 和同一模型配置的只读安全 Trace 核对得到：

| 问法 | Lexical / Dense | Evidence / Confidence | generation | 最终结果 |
| --- | ---: | --- | --- | --- |
| 标准“是什么” | 24 / 24 | 1 条；`ANSWERABLE`，0.55 | 0 次；`BLOCKED_BUDGET` | `extractive_fallback` |
| 口语“是啥” | 24 / 24 | 候选曾形成 3 条，但支持门后为空；`INSUFFICIENT_EVIDENCE`，0.1 | 0 次；`BLOCKED_BUDGET` | 无答案 |

旧知识库已配置 `qwen3.7-flash` 且打开 rewrite，但 campaign 的 generation 为
`24/24`。这说明“本次没有调用模型”不能被解释成“Universal 完全没接 LLM”；
两条请求的真正差异发生在模型调用之前：相同事实候选被不同的问句语法和证据
规则处理。

修复前的独立公开合成 DOCX 也复现：标准问法对同一 chunk 选出 1 条 Evidence，
而“是啥”和“是哪三种”即使 Lexical/Dense 命中同一 chunk，Evidence 仍为 0。
最早偏差由以下组合造成：

1. `answer_support`、Lexical、rewrite guard 和 Product rewrite trigger 分别维护
   不一致的问句正则；普通“是啥”只在部分路径被识别。
2. guard 会全字符串删除“中/和/是/啥/里/的”等字符，既可能把合法口语改写
   误判为范围变化，也可能掩盖实体被改坏。
3. 已接受改写只进入 Lexical；Dense、Rerank、Evidence 和 Confidence 仍消费
   原始分析。
4. 补救 rewrite 仅在早期 Lexical/Exact 全为空时触发；一条无关命中就会阻止
   补救，且 Dense 尚未完成时可能过早调用模型。
5. 原问和近义改写按独立 RRF 通道累加，可能让相同 chunk 在同一逻辑通道刷票。
6. 缓存结果保留创建时的 `generation_mode`，但没有独立字段说明本次请求是否
   真正调用 generation/rewrite，UI 因而容易混淆产物来源与调用事实。

提交前全门禁又发现一个相邻边界：纯关键词“青岛啤酒采购流程”被无条件解析成
`PROCEDURE`，导致原有双索引 Product 测试丢失 Evidence。修复改为只有确认的问句
槽位才识别步骤/流程问答；无问句语法的“流程”保持字面检索。这同样是修规则
边界，不是为题目加特例。

## main / Industry 选择性继承

`main` 与 `Industry` 的旧 `retrieval/rewrite.py` blob 均为
`a9a89860f6cc5ba3e9db5a585679862490052151`。二者都在无历史、无上下文触发、
指代无法解析、锚点漂移、token 超限或输出无效时保留唯一原问；成功时也只返回
原问加一个改写。该旧改写器依赖代词、时序、序号或续问信号，不能解决首轮普通
“是啥”。`Industry` 相比 `main` 还在旧 generation/evidence 层增加了问题显式
主体锚点覆盖，要求模型 claim 和摘录回退都由来源直接覆盖问题主体。

| 决策 | 能力 | 当前处理 |
| --- | --- | --- |
| KEEP | 原问不可覆盖、最多一个改写、失败回退原问 | 作为当前 Product rewrite 不变量保留 |
| KEEP | 有限上下文仅用于消歧、提示和文档只作不可信数据 | 保留现有隔离/预算，不扩建多轮系统 |
| KEEP | Evidence、来源锚点、数字/单位/否定与 KB 权限失败关闭 | 继续由当前 SourceSpan、Evidence、Confidence 和 claim 校验执行 |
| REPAIR | 普通自然问法由多套正则重复解释 | 新增共享类型化语义并让全部下游阶段消费 |
| REPAIR | 仅 `len(hits)==0` 才尝试补救改写 | 改为首轮真实 Evidence/Confidence 不足后最多补一次 |
| ADAPT | 旧 Hybrid 对 q0/q1 各跑 Dense+BM25 | 适配为当前 Lexical+Dense，强制同请求保持一个 selected slot/vector space |
| ADAPT | Industry 旧生成层问题锚点 | 迁移原则到当前 Product Evidence/Confidence，不搬旧 Answer Runtime |
| ADAPT | 多变体 RRF | 同一 chunk 在 lexical/dense 各逻辑 family 只取最佳贡献，仍保留 provenance |
| DEFER | 旧 Runtime 整套多轮上下文和旧 Qdrant 执行器 | 默认入口和数据模型不同，不整体迁回 |
| UNVERIFIED | main/Industry 旧环境运行质量、真实百炼语义质量 | 没有用源码存在或 Mock 结果填质量分数 |

## 最小实现

1. Core 增加 `QuerySemantics`、`RequestedAnswerType` 和带位置的
   `QueryConstraint`，区分 `ENUMERATION/COUNT/ORDINAL_ITEM/DUTIES/PROCEDURE`
   及数量前提、序号、标识符、数值、单位、否定、范围、时间版本和引用原词。
2. Analyzer 只在确认的中文问句边界解析“是什么/是啥/有哪些/哪几种/分别指什么/
   请列出来”等表达；未命中保持 `UNKNOWN`，不把未知直接解释成无答案。
3. rewrite guard 先比较原始硬约束，再比较类型化对象、关系和答案形状；移除对
   “中/和/的/里/啥”等单字的全字符串删除，只在明确的前缀、句尾和“在…方面/
   的职责”等语法位置处理。
4. Planner、Lexical、Dense、Rerank、Evidence、Confidence 和 related contents
   共用 `effective_analysis`；合法改写由 Analyzer 合并语义，但原始问题和硬约束
   仍是权威来源。
5. 首轮完成真实融合、重排、Evidence 和 Confidence 后，仅对
   `INSUFFICIENT_EVIDENCE/AMBIGUOUS_NEEDS_CLARIFICATION` 软失败尝试一次改写；
   权限失败、索引不兼容和来源撤权不能由 LLM 补救。
6. 改写变体同时进入 Lexical 与 Dense；第二次 Dense 若漂移到不同 slot 立即
   fail closed。RRF 按 logical family 去重贡献，避免原问/改写刷票。
7. 对正文列举和结构化列表，Evidence 要求对象、关系导语和连续列表来源闭合；
   列举/计数保留完整列表，序号问法只取导语和指定项，数量前提不符记录
   `SOURCE_CORRECTS_COUNT_PREMISE`，不凑数或截断。
8. `SearchAnswerResult` 增加 `result_origin`、
   `generation_called_this_request`、`rewrite_called_this_request`。缓存命中强制
   本次调用为 0；HTTP 契约校验实际模型、问题、Evidence ID/原文和消息顺序。
9. UI 分开显示模型生成、原文摘录、模型失败摘录回退、缓存、预算、未授权、
   未配置、无证据和需澄清；知识库设置明确提示“选择模型”不等于“已验证/
   已授权/本次已调用”。

## 通用成组回归与质量边界

公开合成语料不使用私有文档、原问题或预期答案映射：

| 类别 | 覆盖 |
| --- | --- |
| 正文列举 | 六种同义表达在真实入库、Lexical/Dense、Evidence、Confidence 和摘录链上共享同一事实集合 |
| 表格/职责/步骤 | 表格关系保护、职责行列闭合、步骤导语加连续列表、序号项与数量问法分离 |
| 条件与硬约束 | 数字、单位、否定、仅限/至少/至多、时间版本、前后顺序、引用原词均不可由改写改变 |
| 名称保护 | `开发中心`、`研发和质量组`、`美的中心`、`啥都有研究组`、`里程碑小组` 及英文连字符标识 |
| 无来源拒答 | “我家在哪”及“Codex 和豆包比谁厉害”在只有蓝鹊工作模式资料时均拒答 |
| 兼容关键词 | 无问句槽位的“采购流程”继续作为字面检索词，不被强制解释成步骤问答 |
| 缓存/故障 | 冷请求、热缓存、未配置、未授权、预算拒绝、Provider 失败、无效输出、旧缓存身份与 KB 隔离 |

阶段可量化的公开合成结果：

- 同一可答事实的六种自然问法：`6/6` 为 `ANSWERABLE`，错误拒答 `0/6`；
  Evidence 都包含三个真实名称。该分母不代表开放域自然语言覆盖率。
- 两个知识库不支持的问题：`2/2` 拒答，错误作答 `0/2`；其中比较题即使有
  Dense 候选也因所问关系无来源支持而拒答。
- 五个含常见虚词的非描述类实体漂移最小对：`5/5` 被 guard 拒绝。
- Mock HTTP 证明生成器实际收到配置模型、当前问题、受控 Evidence 与固定消息
  顺序，并证明冷/热请求调用计数；Mock 不按 question 字符串返回预写答案，
  也不证明真实百炼语义质量。

## 最终 Product 浏览器证据

在实现提交 `bc1ee969...` 上启动临时本地 Product source 服务，使用真实浏览器
完成登录、建项目/知识库、上传公开合成 DOCX、等待索引激活和两次问答：

| 请求 | 候选与证据 | 结果 | 本次模型调用 |
| --- | --- | --- | --- |
| 蓝鹊小组三种工作模式的口语问法 | Lexical 1、Dense primary 1；原文 1 条，含三个名称 | `ANSWERABLE`、`extractive`、有引用 | generation 0；rewrite 0；rerank 0；query embedding 0 |
| Codex 与豆包能力比较 | Lexical 0、Dense primary 1；Evidence 0 | `INSUFFICIENT_EVIDENCE`、`none`、无答案 | generation 0；rewrite 0；rerank 0；query embedding 0 |

第二条证明 Dense 主题近似或任意候选本身不足以发布答案；拒答由通用
Evidence/Confidence 合同产生，不是题目或品牌黑名单。页面同时准确显示
“未配置回答模型”和“当前资料没有足够证据”。临时文档、服务和浏览器标签已清理。

仓库的标准 Playwright runner 另受本机环境阻塞：WSL wrapper 有 CRLF，默认
Chrome 未安装，改用已有 Chromium 后仍缺 `libnspr4.so`。没有安装系统包或修改
宿主环境；真实浏览器验收改由可用的隔离浏览器完成，不把 runner 阻塞写成 E2E
脚本通过。

## 真实百炼调用边界

代码与 Mock 已证明：保存了 generation connection 时，Product resolver 可绑定
回答与改写；每次发送前仍核对 KB 来源、连接验证、出网授权、campaign 预算和
凭据版本。当前只读环境核对得到：

- 本知识库旧配置为 `qwen3.7-flash`、rewrite 已开启；
- 旧 campaign generation 已消费 `24/24`；
- 当前 WSL `DASHSCOPE_API_KEY_PRESENT=no`；
- Docker 只存在生产 `rag_data/rag_secrets` 与无凭据的开发工具卷；
- 在克隆卷创建隔离 campaign 仍需读取生产数据/Secret 卷并短暂停止生产 App，
  超出本轮明确的“不得访问生产卷或 active alias”授权。

因此没有真实冷请求、热缓存和拒答三请求的百炼分母；真实模型正确率、错误拒答率、
组内一致性和不可答错误作答率均为 `BLOCKED / denominator=0`，不能填 0% 或 100%。
恢复条件是提供独立非生产连接/凭据，或用户另行明确授权隔离克隆生产卷的具体
操作；后者还会涉及短暂停止生产容器，不能从“允许免费额度”自动推导。

## Serving、缓存与重建影响

本次只改变查询理解、检索变体、证据选择、回答状态和展示合同：

- retrieval serving implementation 升为 `v3-00-7-semantic-query-v1`；
- rewrite identity 升为 `bounded-rewrite-v3/shared-query-semantics-v1`；
- Product `rewrite_prompt_revision` 和 `answer_selection_revision` 同步升版；
- 旧错误拒答和旧结果不会以同一 cache identity 继续返回。

Parser、`docx-ooxml-v4`、`docx-structural-v3`、Document/Chunk 内容指纹、
Embedding 配方、向量空间和 index fingerprint 均未变化，因此不要求重建文档、
重分块或重嵌入。部署时需要正常构建/重启服务以加载新代码，但本阶段没有执行
镜像构建、生产重启、索引激活或部署；当前生产仍运行旧 revision。

## 实际验证

| 命令/运行 | 退出状态 | 结果 |
| --- | ---: | --- |
| `.venv/bin/python scripts/dev.py doctor` | 0 | Python、WSL Git、源码导入、SQLite FTS5 和临时目录通过 |
| 语义/guard/Product rewrite/公开 E2E 定向集 | 0 | 74 passed、1 个既有 Starlette/httpx warning |
| `.venv/bin/python scripts/dev.py smoke` | 0 | 72 passed、1 warning |
| `.venv/bin/python scripts/dev.py product-check` | 0 | hardcode audit 通过；74 passed、1 warning |
| `.venv/bin/python scripts/dev.py product-smoke` | 0 | 6 passed、1 warning |
| `.venv/bin/python scripts/dev.py check` | 0 | compileall、Ruff lint、360 个 Mypy 源文件、docstring 通过；2649 passed、89 deselected、4 warnings |
| pytest marker collect-only | 0 | 2738 总计；79 `local_integration`、10 `live_provider`、无重叠；默认离线排除 89 |
| 本阶段 Python `ruff format --check` | 0 | 22 个变更文件全部已格式化 |
| 全仓 `ruff format --check .` | 1 / `BASELINE_BLOCKED` | 219 个跨阶段既有文件会被重排；未制造无关批量格式提交 |
| `npm run typecheck` / `npm run lint` | 0 / 0 | TypeScript 与 ESLint 通过 |
| `npm test` | 0 | 17 files、71 tests 通过；保留 1 条预期的 synthetic session stderr |
| `npm run build` | 0 | OpenAPI schema check、TypeScript、Vite 通过；1852 modules，JS 306.63 kB |
| 最终提交上的真实浏览器 Product 闭环 | 0 | 上传/索引/口语回答/引用/无来源比较题拒答与调用详情通过 |
| `git diff --check` / staged diff 安全审计 | 0 | 28 个实现文件；无私有文件名、生产 ID、密钥或 whitespace 问题 |
| 真实百炼 generation/rewrite | `BLOCKED` | 没有独立非生产凭据；未访问生产卷，实际 Provider 请求为 0 |

完整门禁的 4 个 warning 是 1 个既有 Starlette/httpx 弃用提示、2 个测试用
Qdrant 不安全连接提示和 1 个模拟 server version 获取失败提示。89 个 deselected
严格来自 `local_integration/live_provider` marker；本阶段新增测试没有 skip、
deselect 或 xfail。

## 提交与阶段边界

实现提交：

- `bc1ee969cf1cb67b1a1cb7127f412820a917a1f3`
  `fix(retrieval): 统一自然问法与证据拒答`

本文件作为独立文档提交，实际 SHA 以 Git 历史为准。两个提交都只保留在本地
`codex/universal-selective-word-v3`，没有 push、merge、release 或生产部署。

V3-00.7 在“源码工程、离线合同和浏览器产品行为”范围内完成；真实百炼质量仍按
上述恢复条件 `BLOCKED`，不将其包装为全能力通过。本轮到此停止，不进入 V3-01。
