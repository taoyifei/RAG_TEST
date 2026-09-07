# V3-00 Word 与真实流式保护基线

日期：2026-09-07

状态：`BLOCKED`。V3-00 基线与缺口复现已完成；发布安全验证需要对本次
候选镜像的 50 个 High/Critical OS 包版本元组（18 个唯一 CVE、0 个有可用
修复版本）进行人工风险处置，因此 `release acceptance` 按失败即停保持
`NOT_RUN`。

## 范围与基线

- 本文件只记录 V3-00 的能力冻结和缺口复现；不包含后续修复。
- 起点与目标集成分支 `feature/universal-rag` 均为
  `5e21031f72440c0400f3e7798d6b68c64492618d`。
- 只读参照：`main` 为 `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`，
  `Industry` 为 `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。
- 执行分支：`codex/universal-selective-word-v3`。
- 默认离线、无真实 Provider、无数据出网。私有 Word 原件、转换件、图片、
  正文和逐层原始报告均不进入 Git。

## 当前 Product 实际路径

1. 默认 Product Profile 选择 `word-document-v1` Parser 与
   `docx-structural-v3` Chunker；索引构建按 Parser、IR 校验、OCR 增补、
   Artifact 持久化、Chunk、Revision 持久化的顺序执行。
2. 原生 DOCX 由 `docx-ooxml-v4` 解析成结构化 IR，再由现有结构化
   Chunker 生成 citation、embedding、lexical 三视图和 SourceSpan。
3. 二进制 DOC 由受限 `antiword` 提取纯文本，只形成 PARAGRAPH 节点，
   并报告 `LEGACY_DOC_FLATTENED_TEXT`。
4. OCR 媒体扫描只消费已有 `image_attributes`。DOC 扁平化后没有图片节点，
   因而 `[pic]` 不会自动变成媒体或图片正文。
5. 默认 Product 的 `:answer` 在创建 `StreamingResponse` 前同步完成
   `sdk.answer`；前端普通调用发送 `stream: false`，SSE 辅助函数也用
   `response.text()` 整体读取。

## 私有 DOC 分层探针（仅脱敏汇总）

输入的扩展名、OLE magic 与 `application/msword` 一致。探针从默认 Product
组合根取得实际 Parser、ParsingPolicy、OCR Enricher 和 Chunker；运行环境使用
与 Dockerfile 固定版本一致的 `antiword 0.37-17`。主机未安装 `antiword`，因此
这一轮在无网络、当前源码只读挂载的容器内完成。

| 层级 | 状态 | 脱敏证据 |
| --- | --- | --- |
| 格式识别 | PASS | OLE DOC、扩展名与媒体类型一致 |
| Parse / IR | PARTIAL | 8 个节点且均为 PARAGRAPH；8/8 可见文本节点被表示；出现 1 个 `[pic]` 占位 |
| Parse Artifact | PARTIAL | 仅有 source document；没有 embedded media Artifact |
| 图像显示实例 | PARTIAL | 0 个图片节点、0 个可识别显示实例；不能解释为原件已确认无图 |
| 媒体盘点 | PARTIAL | `media_count=0`，但状态明确为 `unknown_after_flattening` |
| OCR | NOT_RUN | 无 `image_attributes` 可供扫描；没有调用真实 OCR 或视觉模型 |
| 关系 | PARTIAL | 0 条关系；没有据占位符推断图中连线或角色语义 |
| Chunk | PASS（文本边界） | 3 个 text chunk；8 个 original-text SourceSpan；覆盖率 1.0、缺失字符 0 |
| 图文完整性 | FAIL | 文本覆盖率为 1.0 仍不代表图片、图片文字或图关系完整 |
| 索引 / 检索 / 回答质量 | NOT_RUN | 本层级探针未据私有内容建立质量结论 |

## 原生 DOCX 公开保护夹具

公开合成 DOCX 同时覆盖多级标题与列表、横纵合并表、重复表头、正文图片、
批注、文本框、同图多处显示、换行和分页、页眉与页脚。冻结快照保留：

- 规范化 DocumentIR、ParseReport 和 Artifact 身份；
- citation、embedding、lexical 三视图及块边界；
- SourceSpan、节点关系、邻居关系和结构上下文；
- 同一媒体的内容身份与两个独立图片显示实例；
- 排除 elapsed time 等运行噪声，同时保留逻辑文档、版本和指纹身份。

同一输入、同一策略连续构建两次结果完全相同；默认 Product 探针也确认实际
选择 `word-document-v1` 与 `docx-structural-v3`。

## 真实 TCP 流式缺口

公开合成知识库通过完整 Product Runtime、Session/CSRF、活动文档版本、
Provider Connection 和一次性预算 Campaign 发起 loopback TCP 请求。慢 Mock
Provider 已进入生成但未释放时，客户端在观测窗口内既收不到响应头，也收不到
首字节；释放后才一次收到 `meta`、`retrieval` 与 canonical `final` 事件。

结论：当前缺口位于 Product API 在 `StreamingResponse` 之前同步等待
`sdk.answer`，不是 TestClient 收集方式造成的假象。该测试冻结当前失败行为；
后续修复阶段应把它改成“生成完成前可见安全阶段事件”的正向门禁。

## Industry 只读参照

在临时导出的只读 `Industry` 提交上，使用当前隔离 Python 环境运行旧回答流
测试得到 4 passed；使用 Node 运行旧前端去重与中断保留测试得到 2 passed。
临时目录已清理，`Industry` 工作树和引用未修改。

这些结果只证明“完整 Claim 通过校验后增量交付、canonical final 不重复、
中断时保留已验证文字”等语义可复现。旧测试没有经过当前 Product Runtime、
Session/CSRF、预算 Campaign 与真实 TCP 端点，故与当前 Product 的首字节时延
`NOT_COMPARABLE`，不填写虚构分数。

## V3-00 能力矩阵

| 能力 | 决策 | 依据与边界 |
| --- | --- | --- |
| 原生 DOCX 结构解析与三视图 Chunk | KEEP | 当前结构、来源和身份快照可重复，不替换现有路径 |
| 二进制 DOC 纯文本提取 | KEEP | 继续作为受限降级能力，但不宣称图文完整 |
| 二进制 DOC 媒体进入 IR | REPAIR | 确认在 antiword 扁平化层丢失，最早修复点在 Chunk 之前 |
| 图片文字与图片关系分离 | ADAPT | OCR 文本不能作为图关系证明；后续只适配语义，不搬旧全局状态 |
| 默认 Product 安全增量 SSE | REPAIR | 真实 TCP 已复现生成结束前无响应头/首字节 |
| 前端增量读取与渲染 | REPAIR | 当前默认非流式，现有 SSE helper 仍整体缓冲正文 |
| Industry 已验证 Claim / 取消语义 | ADAPT | 只读参照语义；是否可组合进新 Runtime 留给后续设计与测试 |
| Dense 多改写召回 | DEFER | 与本阶段 Word/流式基线无直接依赖 |
| 其他文件格式、GraphRAG、Agent、训练平台 | DEFER | 明确不属于 V3-00 与本轮必交付范围 |
| 私有 DOC 的真实 OCR、图理解与回答质量 | UNVERIFIED | 本阶段没有相应出网与费用授权，也没有伪造结论 |

## 验证记录

| 命令 | 退出状态 | 结果 |
| --- | --- | --- |
| `.venv/bin/python scripts/dev.py doctor` | 0 | Python 3.11.15、WSL Git、源码导入、SQLite FTS5 与临时目录通过 |
| `.venv/bin/pytest -q tests/baseline/test_v3_00_word_baseline.py tests/baseline/test_v3_00_product_streaming_baseline.py` | 0 | 5 passed；1 个既有 Starlette/httpx 弃用警告 |
| Word、Chunk、OCR、P09、权限、版本、缓存、预算相关 pytest 集合 | 0 | 176 passed；1 个既有弃用警告 |
| `Industry` 的 `tests/test_answer_streaming.py` | 0 | 4 passed；只读临时导出 |
| `Industry` 的 `node --test tests/frontend_stream_test.mjs` | 0 | 2 passed；只读临时导出 |
| `.venv/bin/python scripts/dev.py check` | 0 | compileall、Ruff、357 个源文件 mypy、docstring 检查通过；最终 2563 passed、88 deselected、4 warnings |
| `.venv/bin/python scripts/dev.py smoke` | 0 | 72 passed |
| `.venv/bin/python scripts/dev.py product-check` | 0 | hardcode audit 通过；74 passed |
| `.venv/bin/python scripts/dev.py product-smoke` | 0 | 6 passed |
| `web-install-check` / `web-lint` / `web-typecheck` | 0 | 锁定依赖完整，ESLint 与 `tsc --noEmit` 通过 |
| `.venv/bin/python scripts/dev.py web-test` | 0 | 17 files、70 tests passed |
| `.venv/bin/python scripts/dev.py web-build` | 0 | OpenAPI check、TypeScript 与 Vite build 通过 |
| `.venv/bin/python scripts/dev.py web-e2e` | 0 | 真实 Chromium：7 passed、3 个按项目配置 skipped |
| `.venv/bin/python scripts/release.py build` | 1 | BuildKit 在发送上下文时错误遍历已忽略的既有 root-only 备份目录；未改该目录 |
| `DOCKER_BUILDKIT=0 .venv/bin/python scripts/release.py build` | 0 | legacy sender 正确应用相同 ignore；候选镜像与 Compose config 通过，Docker 已提示该 sender 将弃用 |
| `.venv/bin/python scripts/release.py verify` | 非 0 / BLOCKED | Python 与 npm audit 均 0 漏洞；镜像用户/工具边界、Secret scan、SBOM 通过；Trivy 为 18 个唯一 CVE、50 个不可修复 High/Critical 包版本元组，缺少绑定本镜像的人工风险处置 |
| `.venv/bin/python scripts/release.py acceptance` | NOT_RUN | `verify` 阻塞后按失败即停未执行 |
| 真实 OCR、视觉理解、远程模型、生产部署 | NOT_RUN | 没有相应数据出网、费用或生产授权 |

代码与证据提交：

- `f3ec112`：离线 Word 分层探针、合成 DOCX 规范快照及媒体未知语义；
- `3519574`：默认 Product loopback TCP 预响应缓冲复现。

两项提交均只包含测试、离线探针或公开合成夹具，没有修改生产 Parser、
Chunker、OCR、检索、回答或前端行为。因而本阶段本身不要求重建已有索引；
后续若修复 DOC 媒体内容，必须创建新版本并重建新索引，不能原地污染旧证据。

## 下一阶段入口

V3-00 只提供可运行基线与根因证据。后续阶段如获授权，应先设计受控 DOC
转换/媒体保留与 Product 安全流式契约，再把本阶段的失败特征转换为正向门禁；
不得用重写 DOCX Parser/Chunker 或关闭权限、预算、来源校验来取得通过。
