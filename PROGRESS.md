# DOCX RAG 交付进度

## 2026-07-28 正式镜像前本地阻塞修复：目标、顺序与风险（8 行）

1. 只修全仓 docstring、真实 DOCX 边界、严格配置、OCR cache key、查询准入和资源回收。
2. 先重建任务 0 基线，再严格按任务 1→5 逐项红测、修复、专项绿测。
3. 默认 docstring 必须覆盖完整 roots，纯 docstring 改动必须通过去除 docstring 后 AST 对比。
4. Parser v3 必须保留内部控制字符、展开普通内容控件、审计 TOC 并拒绝未知证据结构。
5. pipeline schema/元数据词表/RFC3339/重复 JSON key 必须在任何外部资源前 fail closed。
6. OCR 必须按每次请求的 media SHA+revision 做端点级校验；错误结果不得永久缓存。
7. 查询固定运行 4、排队 8、总容量 12，关闭顺序必须先收敛查询再关闭底层客户端。
8. 最大风险是真实 DOCX XML 边界、构造中途所有权转移和断开流生成器的容量释放。

## 2026-07-28 新目标任务 0：事实基线

- Git 基线：HEAD
  `dd997ad517b6b49c2f1a22429e84d35b6ed8d835`；tracked=157、
  untracked=20、modified/deleted=68、完整 status=88、staged=0；当前 diff
  `68 files changed, 3348 insertions(+), 740 deletions(-)`，延续上一目标未提交树。
- 受保护摘要中 `docs`、frozen、results、evidence、既有验收文件及参考仓库
  HEAD/tree/tracked 聚合均与既有基线一致；`artifacts` 当前聚合为
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`，
  与冻结值 `ee2ec...` 不一致，已置顶写入 `BLOCKED.md`，未修改该目录。
- 基线门禁：compileall、Ruff、strict mypy
  `Success: no issues found in 78 source files`、pytest
  `186 passed, 22 warnings in 87.11s`（skipped=0）均退出 0。
- 当前 docstring 默认命令输出 `missing_google_sections=0`，但它只检查 changed；
  `--all` 退出 1 并输出 `missing_google_sections=40`，证明默认全仓语义和严格
  callable 规则均未实现。
- 指定 DOCX 审计命令退出 1：
  `ModuleNotFoundError: No module named 'rag_app'`；安全聚合只读核验为
  documents=6、headings=226、paragraphs=591、tables=71、
  image references=132、unique media=126、blank paragraphs=12，未输出正文或名称。
- 四个 shell、Compose config、`git diff --check`、应用 8 项、OCR 总清单
  69 项、wheels 59 项、models 6 项及冻结集 1 项资产校验均退出 0。
- 临时 index release-safety 退出 0：tracked candidate=175、六类和总
  violations=0；真实 index 前后 SHA 均为
  `5babd6f4638dbf36aec00991569b3dd240f5e7bfb1fbc1d0eeba5541a6508cbf`。
- 调用点：请求级 `threading.Thread` 在 `api/stream.py`，另有 readiness 后台线程；
  `BoundedSemaphore` 仅在 resilience/OCR service；Chunker metadata 仍可为 None；
  配置读取含 pipeline/retrieval/corpus 三套独立 JSON 路径；worker OCR validator
  只校验固定 revision；index fingerprint 当前未序列化 schema_version。

## 2026-07-28 新目标任务 1：全仓 Google Python Style 门禁

- [x] 先新增 `tests/test_google_docstrings.py`；定向红测退出 1，
  `5 failed`，分别证明旧实现漏检完全无 docstring、无参数、`None` 返回、
  无返回注解、中文章节、嵌套 callable，且默认仍错误缩小到 changed、
  不识别显式 `--changed`。
- [x] 检查器默认固定扫描 `src/rag_app`、`evaluation`、`scripts`，只允许
  `--changed` 缩小；`--all` 为同义兼容选项。缺 docstring 同时报告
  docstring/Args/Returns，所有发现稳定排序并保留汇总计数。
- [x] 新规则首次全量退出 1 并暴露 `missing_google_sections=111`；分批只补
  中文 Google docstring 后，默认和 `--changed` 均退出 0，输出
  `missing_google_sections=0`；检查器定向测试为 `5 passed in 0.09s`。
- [x] 对本阶段 30 个只改 docstring 的文件，剥离模块、类、同步/异步函数
  docstring 后比较修改前后 AST SHA256：`docstring_only_files=30`、
  `ast_mismatches=0`。
- [x] README 的 compileall 已包含 evaluation，并写明默认全仓与显式
  `--changed`、临时 `GIT_INDEX_FILE` 发布扫描，以及禁止含
  `__pycache__`/`.pyc`/`.pyo` 的审查 ZIP、Git 候选和 build context。
- [x] 阶段绿测：compileall、Ruff、strict mypy
  `Success: no issues found in 78 source files`、`git diff --check`、
  `git diff --cached --quiet` 均退出 0；全量 pytest
  `191 passed, 22 warnings in 84.06s`，skipped=0，未新增 warning 类别。

## 2026-07-28 新目标任务 2：DOCX 结构边界与 Parser v3

- [x] Parser/Chunk/内容控件红测退出 1，摘要为 `8 failed, 18 passed`；
  失败覆盖纯 `w:tab/w:br` 段落及其空 embedding、普通顶层内容控件漏读、
  TOC 无审计、未知文本/图片节点静默忽略，以及 Chunker/Chunk 接受纯空白。
- [x] 新增按 document order 递归展开的 block iterator：`p/tbl` 保留既有
  语义，普通 `w:sdtContent` 展开，明确标记 `Table of Contents` 的控件跳过
  并计数；未知节点无证据时计数，有非空 `w:t`、图片关系或表格时抛
  `UnsafeDocxError`，不扩展其他 OOXML 范围。
- [x] `_paragraph_text()` 继续保留有效文本内部 tab/换行，但最终
  `text.strip()` 为空即返回空串；Parser、Chunker 和 Chunk 契约三层阻止
  纯空白证据。专项绿测为 `26 passed in 0.38s`。
- [x] `audit_docx_inputs.py` 直接命令的导入路径已修复，输出仅含计数；
  `.venv/bin/python scripts/audit_docx_inputs.py docs` 退出 0：
  documents=6、headings=226、paragraphs=579、tables=71、
  image_references=132、unique_media=126、blank_text_elements=0、
  toc_controls_skipped=3、ordinary_controls_parsed=0、
  unsupported_nodes=15、unsupported_content_with_evidence=0。
- [x] Parser revision 固定为 `docx-parser-v3`，pipeline 与
  `ASSETS.sha256` 已同步。新增 v2 preflight 反测先因构造 Qdrant 退出 1；
  修复后 runtime/worker 在 Qdrant、SQLite、HTTP 前拒绝 v2，
  `tests/test_runtime_preflight.py` 为 `15 passed, 1 warning`。
- [x] 阶段回归：全量 pytest `200 passed, 22 warnings in 79.03s`、
  skipped=0；Ruff、strict mypy（78 source files）、compileall、默认及
  `--changed` docstring、`git diff --check`、8 项应用资产 SHA 均退出 0。

## 2026-07-28 索引兼容契约修复：目标、顺序与风险（8 行）

1. 仅修复索引/服务指纹、语料元数据、改写传递、端点韧性、readiness 与稳定 ID/解析盲区。
2. 先完成任务 0 事实基线，再按任务 1→5 逐项执行红测、修复和专项绿测。
3. index fingerprint 只描述索引兼容性；serving fingerprint 只用于启动诊断和审计。
4. corpus policy 是新索引元数据的唯一来源，语义变化只能通过新 collection 全量重建。
5. resolved query 只驱动软路由和 rerank，原问题仍参与召回并用于最终回答。
6. 每个端点尝试必须包含响应 schema 校验；聊天请求只读 readiness 内存快照。
7. 新 locator 必须消除重复标题、分段和媒体引用碰撞，并补齐表格及嵌套表格图片。
8. 最大风险是旧索引兼容判断、启动前 fail-closed 次序和 DOCX XML 出现顺序被间接测试掩盖。

## 2026-07-28 任务 0：事实基线

- Git：`git rev-parse HEAD` 退出 0，HEAD 为
  `dd997ad517b6b49c2f1a22429e84d35b6ed8d835`；tracked=157、
  untracked=15、staged=0，`git diff --cached --stat` 为空。
- 完整 `git status --short`：修改
  `.gitignore`、`BLOCKED.md`、`Dockerfile`、`PROGRESS.md`、`README.md`、
  `deployment/{.env.example,README.md,compose.yaml,deploy.sh,package.sh,rollback.sh,verify-offline.sh}`、
  `deployment/ocr/Dockerfile`、`design/public/{asset-assembly.md,paddleocr-offline-deployment.md}`、
  `evaluation/{evaluate.py,metrics.py}`、`scripts/load_test_chat.py`、
  `src/rag_app/{__init__.py,manifest.py}`、`src/rag_app/ocr/{__init__.py,main.py,paddle_engine.py}`、
  `src/rag_app/state/store.py` 及 9 个既有测试；删除
  `src/{CACHEDIR.TAG,missing_stubs}`；新增
  `Dockerfile.dockerignore`、`deployment/ocr/{Dockerfile.dockerignore,MODELS.sha256}`、
  `design/public/offline-build-and-server-deployment.md`、
  `evaluation/active_state.py`、4 个 scripts、`src/rag_app/active_evidence.py`
  及 7 个测试/fixture 文件。该状态与上一目标交付树一致。
- 受保护输入聚合 SHA256 复核退出 0：`docs`
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  `artifacts` `ee2ec74eb8cb39e7676ce66deae57e47525e6f69be818d567d40d711553a6415`；
  `evaluation/frozen`
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  `evaluation/results`
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  `design/evidence`
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`；
  既有验收文件
  `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`。
- 只读参考仓库复核退出 0：HEAD
  `03d51db2c0e57ade04c8f9fe035316907d2717f5`、tree
  `84a0a960426da37111a93a806242543c61a881a9`、tracked 聚合
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  `git status --short` 为空。
- ignored/受控资产复核：应用 7 项、OCR 总清单 69 项、OCR wheels 59 项、
  OCR models 6 项、冻结集 1 项均逐项 `OK`；两个 tokenizer SHA 分别为
  `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`
  和 `def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a`。
- 基线门禁均退出 0：compileall；Ruff `All checks passed!`；strict mypy
  `Success: no issues found in 77 source files`；pytest
  `136 passed, 19 warnings in 71.59s`、skipped=0；严格 docstring
  `missing_google_sections=0`；4 个 shell、Compose、`git diff --check` 均通过。
- 临时 Git index 首次因 PowerShell 吞掉 Bash 变量而得到 0 文件并失败；
  改用显式 `/tmp/rag-task0-index-8f02879f` 后退出 0：
  `tracked_files=170`、六类违规及总违规均为 0；真实 index 前后均为
  `5babd6f4638dbf36aec00991569b3dd240f5e7bfb1fbc1d0eeba5541a6508cbf`。
- 资产组合命令首次因错误相对目录在冻结集步骤退出 1；拆分后
  `evaluation/frozen/MANIFEST.sha256` 输出 `dataset.json: OK` 并退出 0。
- 调用点搜索前两次因 Windows `rg` 回退/PowerShell 管道解释退出 1；改用
  PowerShell `Select-String` 后退出 0。`pipeline.fingerprint()` 位于
  `contracts.py:222`、`worker_runtime.py:140`、`assets.py:71`、
  `runtime.py:108`、`job_runner.py:94`，以及 11 个测试/fixture 和
  `evaluation/chunking_experiment.py:82`；`pipeline_fingerprint` 生产代码分布于
  contracts、active_evidence、observability、chunking、manifest、assets、
  health、runtime、API、state、index 和 CLI。
- 其余调用点：`max_model_concurrency` 位于 `settings.py:185`、
  `worker_runtime.py:111`、`runtime.py:380`；`QueryVariants` 位于
  `rewrite.py:82/116/138/180`、`hybrid.py:95` 和 query/hybrid 测试；
  readiness 实际调用为 `api/app.py:126/148`，探针调用为
  `health.py:261`；`stable_chunk_id()` 定义/生产调用位于
  `contracts.py:297`、`active_evidence.py:423`、`chunking.py:178`，
  另有 active-evidence/contracts 测试调用。

## 2026-07-28 任务 1：index/serving fingerprint 与启动前契约

- [x] 红测一：`pytest -q tests/test_pipeline_contracts.py` 退出 1，
  `2 failed, 2 passed`；旧实现把 prompt/reranker/LLM 纳入索引指纹，且没有
  `serving_fingerprint()`。
- [x] 红测二：`pytest -q tests/test_runtime_preflight.py` 退出 1，
  `7 failed`；tokenizer、parser、BM25、prompt 和 model ID 错配均先构造
  Qdrant 客户端，证明没有启动前 fail-closed。
- [x] `PipelineSpec.index_fingerprint()` 只序列化 parser、OCR、chunker、
  embedding tokenizer/文档 instruction、BM25、Qdrant revision 和 corpus
  policy 摘要；兼容入口 `fingerprint()` 返回同一索引指纹。
- [x] `RetrievalSettings.serving_fingerprint()` 覆盖 index fingerprint、
  改写/召回/RRF/metadata/软路由、reranker/相邻块/证据与输出预算、LLM、
  prompt 和 LLM tokenizer；`status`、字段顺序和 JSON 排版不进入语义摘要。
- [x] `load_pipeline()` 要求 JSON 显式提供全部字段并拒绝未知字段；manifest、
  Qdrant payload、任务及活动证据的 `pipeline_fingerprint` 未改名，仍只保存
  index fingerprint；serving fingerprint 仅进入结构化审计日志。
- [x] runtime 在 Qdrant/SQLite/HTTP 客户端创建前校验两个 tokenizer 文件 SHA、
  BM25 tokenizer/language/revision、embedding/reranker/LLM model ID 和实际
  QueryRewriter+AnswerGenerator prompt 组合；worker 同阶段校验 embedding
  tokenizer/model、DocxParser、BM25 及 OCR 置信度契约。
- [x] worker 的 `DocxBuildConfig.embedding_instruction` 已改为
  `pipeline.document_embedding_instruction`，不再硬编码空串；专项测试直接
  捕获构建配置并证明非空指令能完整传递。
- [x] 旧版“全字段 pipeline”指纹被 `IndexManifest` 明确拒绝；配置固定
  LLM tokenizer SHA
  `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`
  与 embedding tokenizer SHA
  `def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a`。
- [x] 行为绿测最终 `19 passed, 1 warning in 1.05s`；专项 Ruff
  `All checks passed!`；strict mypy
  `Success: no issues found in 5 source files`。扩展回归首次因误写不存在的
  `tests/test_runtime_query_integration.py` 在收集前退出 4；改用实际文件后
  `25 passed, 1 warning in 2.06s`。
- [x] worker 指令测试首次因夹具不是合法 tokenizer 退出 1，并伴随 Ruff
  两项 `B009`；改成真实最小 tokenizer 和明确类型后转为上述 19 项全绿。

## 2026-07-28 任务 2：唯一 corpus policy 元数据来源

- [x] 红测：`pytest -q tests/test_corpus_policy.py` 退出 2，收集阶段
  `ImportError: cannot import name 'DocumentMetadata'`，证明旧实现没有语料
  元数据契约或 policy 加载器。
- [x] 新增严格只读 `deployment/config/corpus-policy.json`：
  schema v1、`active/official` 默认值、空有效期和空覆盖，不含真实私有文档名；
  `RuntimeSettings.corpus_policy_path` 默认指向镜像内固定路径。
- [x] `CorpusPolicy` 拒绝未知/重复 JSON 字段、绝对路径、`..`、反斜杠、
  非规范路径、重复项、大小写折叠冲突、符号链接、越界、多余覆盖、
  无时区/倒置日期和 `unspecified`；覆盖只能命中本次发现的 DOCX。
- [x] policy 语义摘要把日期规范化到 UTC、按路径排序并忽略 JSON 排版；
  当前摘要为
  `1079f1dae19d0e134f5660234eab9961e68adbb6724ea8d6f0de81db42646c61`，
  与 pipeline 显式字段一致，语义变化会改变 index fingerprint。
- [x] worker 在 Qdrant、SQLite 和 embedding 客户端创建前发现 DOCX、验证
  policy 摘要及全部覆盖；未知覆盖专项证明 Qdrant/embedding 尚未构造即失败。
  runtime 同样在外部资源创建前验证 policy 文件和摘要。
- [x] `DocxChunkBuilder` 要求每个 source 有已解析元数据，并在调用 Chunker
  创建 chunk 前传入；Chunk/Qdrant payload 完整保存 status、authority 和
  两个有效期，新 chunk 契约拒绝 `unspecified`。
- [x] 真实 Qdrant metadata filter 使用 `active/official`，证明默认活动文档
  可检索，draft、过期和非权威文档被排除；checked-in retrieval filter 已移除
  `unspecified`。
- [x] 真实 Qdrant 全量/增量反向测试证明：policy 摘要变化后的增量任务失败，
  alias 仍指向旧 collection 且旧点数不变；随后全量任务创建不同物理
  collection 并原子切换 alias，未原地修改旧 payload。
- [x] 绿测：策略/前置失败/DOCX build/metadata/Qdrant job/fingerprint 合计
  `42 passed, 5 warnings in 19.11s`；Ruff `All checks passed!`；
  strict mypy `Success: no issues found in 7 source files`；更新后的 8 项
  `deployment/ASSETS.sha256` 全部 `OK`。

## 2026-07-28 任务 3：resolved query 传播边界

- [x] 红测：改写、查询服务和混合检索专项退出 1，
  `3 failed, 2 passed, 1 warning in 3.66s`；旧触发器把普通直问中的“该”
  误判为上下文依赖，reranker 和软路由都收到原问题。
- [x] `QueryVariants` 现在显式携带唯一 `resolved_query` 并校验语义：
  未改写时等于原问题，成功改写时等于第二个且唯一的独立问题；失败回退仍只
  保留原问题。
- [x] 触发器只保留明确的代词/省略信号，普通“设备过热该如何处理”和
  “变压器油温过高怎么办呢”不调用 LLM；“昨天那个跳闸的怎么处理”和
  “3号主变那个告警呢”在有历史时改写，无历史时不调用。
- [x] soft route 与 reranker 只接收 `resolved_query`；dense/BM25 仍保留
  原问题和独立改写问题两个通道；最终 AnswerGenerator 仍接收用户原问题。
- [x] QueryRewriter prompt revision 更新为
  `sha256:aafa8168c32451b266bc78e3ca4719830870c2a5568ef5475f3242d7bc553efe`，
  组合 prompt revision 更新为
  `sha256:9fc5318f48fe38a5941cf6b8738c9725dcc3aebaa5f55bc4b698ecf55e4398d7`；
  pipeline 资产摘要同步更新并 8/8 `OK`。
- [x] 绿测：定向回归
  `29 passed, 2 warnings in 4.01s`；Ruff `All checks passed!`；
  strict mypy `Success: no issues found in 5 source files`。Ruff 首轮唯一红灯为
  `QueryVariants` 的查询数量魔法数字，改为具名常量后全绿。

## 2026-07-28 任务 4：端点级 schema 韧性与缓存 readiness

- [x] 主红测退出 1，`18 failed, 13 passed, 1 warning in 1.66s`：
  `request_json()` 尚无 validator，embedding 无 model 契约，reranker/LLM/OCR
  在 pool 成功后才发现坏响应，readiness 每次现场探测，且只有共享
  `max_model_concurrency`。
- [x] `ResilientHttpPool` 把 HTTP、JSON 和服务 schema 校验纳入同一次端点
  attempt；schema 失败计入熔断并切换，只有全通过才记录成功。最终错误只暴露
  `HTTP_TRANSPORT/HTTP_n/INVALID_JSON/INVALID_RESPONSE_SCHEMA/
  NO_HEALTHY_ENDPOINT` 等稳定类别，不带请求或响应内容；明确 4xx 立即终止且
  不切换。
- [x] embedding 校验 model、条数、完整唯一 index、固定维度、真实数值类型及
  finite；reranker 校验条数、index 和 `[0,1]` finite score；LLM 校验 model、
  单 choice、`finish_reason=stop`、非空 content 和一致 usage；worker 的 OCR
  pool 校验 Pydantic schema 与冻结 revision。
- [x] wrong-model、错误维度、错误 finish_reason、错误 rerank score 和错误
  OCR revision 均切到下一端点；全部 schema 错误后端点进入熔断；4xx 反测只
  调一次。合法 LLM completion 返回后，引用业务校验仍在 pool 外，不会把业务
  失败当作端点失败重放。
- [x] 将共享闸门拆为 `max_embedding_concurrency`、
  `max_reranker_concurrency`、`max_llm_concurrency`，默认均保持 5；
  `max_ocr_concurrency` 保持且只能为 1，`.env.example` 已列出四项。
- [x] `ReadinessService.start()` 启动时同步刷新一次，再由唯一后台线程按固定
  间隔刷新；`/ready` 与 `/api/chat` 只读加锁快照。未刷新、超过 max
  staleness 和后台异常均 fail closed，异常详情不进入状态。
- [x] 计数器/假时钟证明连续 ready/chat 不增加探针调用，缓存过期时也不现场
  探测；线程测试证明后台确实刷新且 `close()` 后终止。RuntimeBundle 顺序测试
  固定为 readiness stop/join → HTTP clients → Qdrant。
- [x] 第二个红测拒绝可转换字符串向量，旧实现
  `Failed: DID NOT RAISE ExternalServiceUnavailableError`；改为只接受 JSON
  number 后转绿。
- [x] 绿测扩展回归 `52 passed, 3 warnings in 4.75s`；专项 Ruff
  `All checks passed!`；strict mypy
  `Success: no issues found in 7 source files`。Ruff 首轮仅
  `TeiEmbeddingClient.__init__` 参数过多，收敛为
  `EmbeddingClientConfig` 后全绿。

## 2026-07-28 任务 5：完整 Locator、稳定 ID 与表格图片

- [x] 红测退出 1，`9 failed, 16 passed, 8 warnings in 23.27s`：
  Locator 丢弃 heading/segment 字段，相同长文本的 5 个重叠 segment 只得到
  1 个 chunk ID，重复标题碰撞，tab/换行被拼接，表格与嵌套表格两张图片均
  漏失，Qdrant locator payload 也没有新字段。
- [x] `Locator` 新增可持久化 `heading_index`、`segment_index`，两者进入
  `logical_key()`；display 在存在时增加“标题N/片段N”，没有引入页码或修改
  既有 HTTP API schema。
- [x] `DocxParser.version` 更新为 `docx-parser-v2`；每个标题按文档顺序编号，
  后续段落、表格和图片继承当前标题编号。pipeline parser revision 与资产
  SHA 已同步，旧 `docx-parser-v1` 在 Qdrant/HTTP/SQLite 构造前被拒绝。
- [x] Chunker 对每个元素的全部 segment（含唯一 segment）从 1 稳定编号，
  再用完整 locator 计算 chunk ID 并写入 payload；重复相同/overlap 文本的
  ID 全唯一，纯文件重命名仍不改变 element/chunk ID。
- [x] DOCX 图片关系改为单次 XML document-order 查询；正文段落、正文表格
  单元格及嵌套表格图片均被发现。相同媒体的多次关系引用保留 image locator
  1/2/3 和唯一 element ID，媒体 SHA 相同，后续相同 OCR 文本仍得到两个唯一
  chunk ID，未改变按媒体 SHA 的 OCR 缓存键。
- [x] 段落 XML 按节点顺序保留 `w:tab` 为 `\t`、`w:br/w:cr` 为 `\n`；
  未扩展页眉页脚、脚注、批注、文本框、SmartArt、OLE 或 EMF 转换。
- [x] Qdrant payload 的 locator JSON 由完整模型导出；active evidence 要求
  新字段存在，并用新 logical key 重算 chunk ID。真实 Qdrant 反测删除两个
  字段后导出失败；新 locator 正常分页导出。
- [x] 相同输入重复解析/切块结果完全一致；重复标题、重复 segment、重复媒体
  的 element/chunk ID 均唯一；表格图片、嵌套图片、tab/换行、重命名均有
  明确断言。
- [x] 最终专项行为回归 `50 passed, 9 warnings in 24.43s`；Ruff
  `All checks passed!`；strict mypy
  `Success: no issues found in 4 source files`；8 项应用资产 SHA 全部 `OK`。

## 2026-07-28 索引契约修复最终验收

- [x] 第二轮完整门禁均退出 0：
  `.venv/bin/python -m compileall -q src tests scripts evaluation`；
  全仓 Ruff `All checks passed!`；strict mypy
  `Success: no issues found in 78 source files`；全量 pytest
  `186 passed, 22 warnings in 80.96s`、skipped=0，超过任务 0 的 136 项基线。
- [x] Google docstring 首次误用 `--all` 扫描到 65 个任务 0 基线之外的历史
  缺口并退出 1；按基线相同的“本工作树新增/修改非测试 Python”严格口径运行，
  首次为 25 项，本轮白名单内补齐后
  `.venv/bin/python scripts/check_google_docstrings.py` 输出
  `missing_google_sections=0` 并退出 0；未修改白名单外历史文件。
- [x] `bash -n deployment/{deploy,package,rollback,verify-offline}.sh`、
  `docker compose --env-file deployment/.env.example -f
  deployment/compose.yaml config -q`、`git diff --check` 均退出 0。
- [x] 受控资产全部通过：应用 8 项、OCR 总清单 69 项、wheels 59 项、
  models 6 项、冻结集 1 项全部 `OK`；pipeline 新 SHA 为
  `4c4dfcd972d8f43dfbb16c4a8f29bc641979d90a27634d2200ff22b8ec9a4835`。
- [x] 临时 Git index 发布安全扫描退出 0：tracked candidate=175，
  private path/network/local path/secret/binary/large file 和总 violations 均为
  0；真实 `.git/index` 前后 SHA 均为
  `5babd6f4638dbf36aec00991569b3dd240f5e7bfb1fbc1d0eeba5541a6508cbf`。
- [x] 受保护摘要与任务 0 完全一致：`docs`
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  `artifacts` `ee2ec74eb8cb39e7676ce66deae57e47525e6f69be818d567d40d711553a6415`；
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`；
  验收文件 `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`。
- [x] 只读参考仓库仍为 HEAD
  `03d51db2c0e57ade04c8f9fe035316907d2717f5`、tree
  `84a0a960426da37111a93a806242543c61a881a9`、tracked 聚合
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  worktree 为空。
- [x] 当前 HEAD 仍为任务 0 的
  `dd997ad517b6b49c2f1a22429e84d35b6ed8d835`，staged=0；本目标没有
  commit/push、联网下载、服务器访问、镜像 build/save/package。外部证据缺口
  已完整保留在 `BLOCKED.md`。

## 2026-07-28 双包离线交付修复开工回执

1. 当前 HEAD `dd997ad`，main 干净、5 commits、157 tracked、无 remote。
2. compileall、Ruff、mypy 72 files、四脚本、Compose、冻结 SHA、diff 均退出 0。
3. pytest `115 passed, 12 warnings in 53.69s`，skipped=0。
4. release safety：157 tracked，六类违规及总 violations 均为 0。
5. Python 3.10 导入 OCR 红灯：`enum.StrEnum` ImportError。
6. 旧 `docx-rag:0.1.0` 为 `f85c569e...`，无 OCI revision label。
7. 旧 wheel 含 `worker_runtime.py`，但完全缺少 `rag_app/ocr/**`，不得复用。
8. 顺序：可信根→OCR 隔离→可复现双包→服务器布局/教程→docstring/全门禁。
9. 最大风险是活动 Qdrant 现场与 SQLite manifest 的跨存储一致性验证。

### 任务 1：活动证据可信根

- [x] 生产 evaluator/load test 已删除自由证据 JSON 输入，改为从活动
  Qdrant alias、SQLite active manifest 和物理 collection 现场分页生成。
- [x] 清单固定 collection、index manifest SHA、pipeline、point count、
  records SHA 和整体 SHA；SQLite manifest、文本、locator/chunk ID 摘要均重算。
- [x] 协同伪造红证据：新增测试最初
  `Failed: DID NOT RAISE TypeError`，证明 results+manifest 可一起假通过。
- [x] 真实 Qdrant 绿证据：跨页无重复、locator/text/hash 篡改、旧
  collection、旧 pipeline、retired point 与 SQLite 摘要共 `22 passed`；
  Ruff 全绿，mypy `no issues found in 6 source files`。

### 任务 2：OCR Python 3.10 隔离与结果正确性

- [x] 红证据：完整源码/最小 COPY 树均因 eager import 失败；NumPy bbox 为
  `(0,0,0,0)`；OCR 服务恢复后的第二次构建仍没有 OCR chunk。
- [x] `rag_app` 与 `rag_app.ocr` 改为 PEP 562 懒加载；`main` 只在启动时
  解析第三方依赖。Python 3.10 两种导入硬门禁均通过，最小树无 contracts.py。
- [x] `rec_boxes` 支持 NumPy `tolist()` 并校验有限数值；瞬态
  `OCR_SERVICE_UNAVAILABLE` 结果仍留审计状态，但不再作为永久缓存命中。
- [x] 绿证据：专项 `4 passed`，Ruff 全绿，mypy `no issues found in 2 files`。
- [x] 现有 OCR 资产 manifest 69 项全部 OK；真实 CPU 冒烟输出：
  `51 lines / 189 chars / confidence 0.943705 / inference 17.293s`，未输出原文。

### 任务 3：可复现构建与 runtime/corpus 双包

- [x] 新增 Python 3.11 runtime wheel 准备入口；只下载 linux/amd64
  固定 wheel，重建当前项目 wheel，并硬校验 `ocr/**` 与
  `worker_runtime.py` 后生成 `WHEELS.sha256`。
- [x] app Dockerfile 改为实际按 runtime lock 从本地 wheelhouse 安装并
  `pip check`；app/OCR 均要求 40 位 OCI revision，OCR 构建逐文件校验
  `MODELS.sha256`。
- [x] `package.sh` 改为 runtime/corpus 双包；两包均有内部逐文件 manifest
  和外层 `.tar.gz.sha256`，三张镜像固定分别保存/加载，不遍历任意 tar。
- [x] 红证据：旧 wheel 缺 OCR、错误模型摘要、错误外层 SHA、路径穿越和旧
  package revision 契约均失败；绿证据：专项 `8 passed`，Ruff、mypy、
  四脚本 syntax 与 Compose config 均退出 0。
- [x] 按目标边界未执行 download/build/save/package；真实产物摘要留给用户
  按公开手册回填，未把旧 `docx-rag:0.1.0` 当成新候选。

### 任务 4：服务器目录、部署教程与回滚

- [x] Compose 删除 named volumes，改为
  `RAG_STATE_PATH`、`RAG_QDRANT_PATH`、只读 `RAG_DOCS_PATH` bind mount。
- [x] 固定 `/data/tyf/RAG` 的 incoming/releases/current/shared/data/
  backups/logs 布局；env、current 和 rollback 记录均在 release 外。
- [x] 部署脚本只加载 manifest 点名的三张镜像，复核 linux/amd64 与 OCI
  revision，再执行 `up -d --no-build --pull never`；回滚不删状态或语料。
- [x] `design/public/offline-build-and-server-deployment.md` 已覆盖三张固定
  digest 基础镜像、资产准备、断网构建、双包、`${RAG_SERVER}` 上传、安全
  解包、权限、GPU/基础设施冒烟、冻结后验收、备份与回滚；不含真实服务器 IP。
- [x] `design/public/paddleocr-offline-deployment.md` 已补为稳定入口，解决
  原先 README 引用文件不存在的问题。

### 工程卫生与 Google Python Style

- [x] 删除 tracked `src/CACHEDIR.TAG` 与 `src/missing_stubs`，并仅精确忽略
  `/src/CACHEDIR.TAG`、`/src/missing_stubs`。
- [x] 新增 AST 机械门禁；当前新增/修改的非测试 Python 输出
  `missing_google_sections=0`，未通过拆模块规避检查。

### 2026-07-28 最终本地验收

- [x] 第二轮完整门禁：compileall 退出 0；Ruff `All checks passed!`；
  mypy `Success: no issues found in 77 source files`；pytest
  `136 passed, 19 warnings in 69.00s`，skipped=0。
- [x] 四个 deployment shell `bash -n`、Compose config、Google docstring
  门禁和 `git diff --check` 均退出 0。
- [x] app 7 项资源、OCR 总资产 69 项、OCR wheels 59 项、模型 6 项和
  frozen dataset manifest 全部 SHA256 `OK`。
- [x] OCR 模型首次因在 `deployment/ocr` 错误相对目录执行而 6 项
  `FAILED open or read`；修正 package/手册到 `deployment/ocr/assets` 后
  6/6 `OK`，专项保持全绿。
- [x] 发布安全候选首次仅命中教程中的个人 WSL 路径 1 项；改为
  `RAG_REPOSITORY` 后最终复核 170 tracked candidate、六类和总
  violations 均为 0。
- [x] 真实只读输入复核仍为 6 DOCX、22,358,173 bytes、71 表格、
  132 图片引用和 126 唯一媒体；未修改或暂存私有语料。
- [x] Git index 无改动、remote 为空、push=0；按用户补充要求，本目标完成后
  保持全部代码未 commit。

## 2026-07-27 源码发布与 PaddleOCR 开工回执

1. 新目标是源码安全提交、P0 评测可信、PaddleOCR 代码和离线资料齐全。
2. 先修 Git 忽略与脱敏，再修 evaluator/负载 P0，最后实现 OCR 和发布资料。
3. pytest 基线 `99 passed`、skipped=0；Ruff 与冻结 manifest 均通过。
4. 扩展 mypy 精确复现 `scripts/audit_docx_inputs.py:58` 唯一错误。
5. `src/.gitignore` 确认把全部源码误忽略；只删除任务书点名的三类生成物。
6. GitHub `ls-remote` 60 秒超时，已置顶阻塞；不配置 remote、不 push。
7. 禁止 SSH、Docker build/save、上传；OCR 仅允许本地 CPU 真测与资料产出。
8. 最大风险是发布扫描漏掉私有语料/IP，以及 OCR 资产许可证与离线完整性。

### 只读范围 SHA256 基线

- `docs/`：`36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`
- `artifacts/`：`ee2ec74eb8cb39e7676ce66deae57e47525e6f69be818d567d40d711553a6415`
- `evaluation/frozen/`：`63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`
- `evaluation/results/`：`cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`
- `design/evidence/`：`05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`
- `design/acceptance-and-offline-deployment-2026-07-27.md`：
  `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`
- 只读参考仓库 HEAD/tree：
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9`；tracked 内容聚合 SHA256：
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  tracked 工作区状态为空。

### 本轮已完成

- [x] 修复扩展 mypy 的唯一基线错误：DOCX 审计脚本改为在遍历时累加
  `total_bytes`，不再把 `dict[str, object]` 值盲转为 `int`。
- [x] 建立候选 Git 内容发布安全检查。
  - 反向红证据：首次收集因 `scripts.check_release_safety` 不存在而报错。
  - 绿证据：私有路径、RFC1918、本机路径、凭据、大文件和二进制专项
    `2 passed`；检查结果输出稳定 JSON 分类计数。
- [x] 修复 evaluator 信任结果自报 chunk 合法性的问题。
  - 结果 schema 已删除 `invalid_citation_ids`；CLI 强制接收独立活动证据
    manifest。
  - 合法引用必须同时存在于本题 reranked 与活动 manifest，并逐项核对
    chunk ID、文档路径、locator 和 quote；检索指标也排除伪造映射。
  - 反向红证据：活动 manifest 类型不存在，专项收集报 ImportError。
  - 绿证据：伪造 chunk ID 被自算为 1 个无效引用并使门槛失败；结果尝试
    自报 invalid 字段会被 `extra=forbid` 拒绝。
- [x] 修复全拒答可假通过的负载统计。
  - 分离 answered、正确/错误拒答、意外回答、无效引用、HTTP、解析和协议错误；
    错误拒答、意外回答或无效引用任一非零均不能通过。
  - 冻结 case 中已有历史问题会在同一 conversation 下预热后再问目标题，
    报告独立记录 history request 与 multiturn case。
  - 反向红证据：首次运行 `2 failed, 1 passed`，缺少新结果模型和引用分类。
  - 绿证据：P0、发布检查合并专项 `13 passed in 0.16s`；Ruff 全绿；
    扩展 mypy `Success: no issues found in 61 source files`。
- [x] 固定并校验 PaddleOCR 官方模型资产来源。
  - 下载器首次红灯为 `BinaryIO` 导入错误；修复后专项 `2 passed`，
    Ruff 全绿、mypy `no issues found in 2 source files`。
  - 可重入执行输出 `assets=3, models=2`；det/rec 归档与
    PaddleOCR 3.5.0 许可证按固定 URL、字节数和 SHA256 校验后安全解包，
    本地产物全部留在 ignored `deployment/ocr/assets/`。
- [x] 实现独立 PaddleOCR 服务、worker 客户端与媒体状态接缝。
  - 固定 3.5.0、PP-OCRv5 server det/rec、`paddle_static`、fp32；
    禁用 VL/HPI/TRT/方向与去扭曲模型，单并发，输入/像素/超时受限。
  - API 校验 Bearer token、媒体 SHA256、revision、真实图片格式与响应 schema；
    EMF 只通过无 shell、受 CPU/内存/文件/输出/超时限制的转换器接口。
  - 专项覆盖 API、客户端、固定引擎参数、EMF、构建器和配置，共
    `11 passed` 后新增安全用例仍为 `7 passed`；Ruff/mypy 全绿。
- [x] 完成允许范围内的真实 CPU OCR 冒烟。
  - 首次推理真实失败于 Paddle oneDNN/PIR
    `ConvertPirAttribute2RuntimeAttribute`；显式禁用 MKL-DNN 后复测成功。
  - 一张真实 DOCX 内 PNG：51 行、189 个非空白字符、均值置信度
    0.943705，模型加载 1.632 秒、CPU 推理 12.854 秒；未输出识别原文。
- [x] 固定 OCR 镜像的 Python 3.10 离线依赖。
  - 官方基础镜像 digest 为
    `sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776`，
    linux/amd64 且内置 Python 3.10；主应用仍保持 Python 3.11。
  - 3.11 冻结集直接用于 3.10 首次因 `networkx 3.6.1` 不兼容而红；
    改用 3.10 解析后固定 59 个 wheels。下载器输出
    `verified_wheels=59`，模型+wheel 总 manifest 与独立 wheel SHA 全绿。
- [x] 公开源码测试已脱离私有语料和 ignored 资产。
  - 运行时合成 DOCX、60 题（45 tuning/15 holdout、10 类、5 OCR）和极小
    tokenizer；相关 `16 passed`，未改 `evaluation/frozen/**`。
- [x] 补齐 OCR Dockerfile、Compose、离线包/校验/回滚设计和公开手册。
  - Compose config 退出 0：OCR 单 GPU、非 root、只读、内部网络且不发布端口；
    只有 worker 依赖 OCR，查询应用可继续服务旧索引。
  - 四个服务器脚本 `bash -n` 通过；包设计包含三镜像、评测运行时、
    checksum、三份 SBOM、PaddleOCR/NVIDIA 许可证与来源记录。
- [x] 第一次完整本地验收通过。
  - compileall、Ruff、扩展 mypy 72 文件、四脚本、Compose 和冻结 manifest
    均退出 0；pytest `115 passed, 12 warnings in 47.67s`、skipped=0。
  - 临时 Git index 首次发现 4 个凭据误报，修正为单行且区分引用表达式后，
    反向硬编码测试仍红、专项 `3 passed`；发布扫描 157 个文本文件，
    六类违规和总 violations 全为 0，cached diff check 退出 0。
- [x] 只读范围终检未漂移。
  - `docs`、`artifacts`、冻结集、results、既有 evidence、既有验收报告的
    聚合/文件 SHA256 均与开工基线逐项相同。
  - 参考仓库 HEAD/tree/受跟踪内容聚合仍为
    `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
    `84a0a960426da37111a93a806242543c61a881a9` /
    `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
    tracked 工作区状态为空。
- [x] 按当前目标创建本地分组提交，不配置 remote、不 push。
  - 发布边界：`4168d68`。
  - RAG 核心、可信评测与 OCR 接缝：`ccb9d66`。
  - 公开合成测试与安全反向验证：`f1d736c`。
  - 离线部署链：`201542e`。
  - 本文件、公开手册和阻塞记录由最后的文档提交交付。

## 2026-07-27 运行时配置补充

- [x] 补齐生产运行时配置与“未冻结参数不可 ready”门禁。
  - 查询、管理、Qdrant 三类密钥均无代码默认值且长度至少 32；查询与管理令牌
    相同会在配置加载时拒绝。所有端点必须是无凭据/query/fragment 的 HTTP(S)
    URL，端点列表非空、去重。
  - 检索配置使用严格 JSON schema；`provisional` 状态使
    `retrieval_configuration` readiness 明确失败，只有冻结集标记为
    `frozen` 才可就绪。
  - 红证据：新增用例首次收集失败
    `ImportError: cannot import name 'FrozenConfigurationProbe'`。
  - 绿证据：配置专项 `3 passed in 0.12s`；Ruff 全绿；strict mypy
    `no issues found in 41 source files`。
- [x] 接通生产查询运行时与不可绕过的 readiness。
  - 运行时组装 Qdrant alias、TEI embedding、reranker、四端点 LLM、
    条件改写、混合召回、精排、证据预算、严格回答和 TTL 会话。
  - chat 在鉴权后仍必须通过“参数冻结 + Qdrant + 活动 alias/manifest +
    embedding + reranker + 至少一个 LLM”全部门禁；直接调用 API 不能绕过
    `/ready`。已存在但不兼容的 alias/manifest 在构造运行时时立即拒绝。
  - 专项门禁 `10 passed, 1 warning in 1.19s`；Ruff 全绿；strict mypy
    `no issues found in 42 source files`。
- [x] 固化并验证离线 tokenizer、配置和静态资源。
  - LLM tokenizer SHA256
    `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`；
    embedding tokenizer SHA256
    `def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a`，
    均与只读参考源一致。
  - `ASSETS.sha256` 覆盖 2 份配置、2 份 tokenizer 和 3 个前端文件；
    路径越界、重复项、摘要漂移或远程前端资源均拒绝。
  - 红证据：首次收集 `ModuleNotFoundError: rag_app.assets`；绿证据：
    `2 passed in 0.99s`。本地 CLI 自检输出
    `verified_files=7`、`tokenizer_probe_tokens=11`。
- [x] 本地构建第一版 linux/amd64 应用镜像并通过断网资源自检。
  - 初始在线 Docker 构建三次分别因 Docker Hub 授权超时及 wheel 层超时
    未完成；按规则换为 WSL 预下载 39 个固定 wheel、Docker RUN
    `--no-index` 的离线路径。
  - 实际命令
    `docker buildx build --network none --platform linux/amd64 --load
    --tag docx-rag:0.1.0 .` 退出 0；镜像
    `docx-rag@sha256:2cd75736736d04585ce336e8a24093a2f7d72c11a19d4cb5f266b7b678ab4a78`，
    amd64/linux，`166,565,881` bytes。
  - `docker run --rm --network none docx-rag:0.1.0 asset-selfcheck`
    退出 0，输出 7/7 资源校验通过；当前检索参数如实为
    `provisional`，所以生产 ready 仍会失败，不冒充冻结指标。

更新时间：2026-07-27（Asia/Hong_Kong）

## 目标、顺序与最大风险（8 行）

1. 只交付 DOCX 自研最小 RAG；RAGFlow v0.26.4 仅作 ADR 对照。
2. 先冻结输入、依赖与 pipeline fingerprint，再改契约和结构切块。
3. 再做 SQLite WAL 单写者、Qdrant v1.18.3 staging/alias/snapshot。
4. 再做 dense+中文 BM25/RRF、rerank、条件改写和严格引用拒答。
5. 冻结不少于 50 题且至少 15 题 holdout 后，才用消融确定参数。
6. 最后完成 API/日志/极简页、离线镜像包和断网/性能/恢复验收。
7. 最大本地风险是中文 sparse 分词、结构切块和严格引用指标未验证。
8. 最大外部风险是 `.60` 无 SSH 身份，阻塞 GPU OCR 和最终部署实证。

## 当前阶段

任务 0 外部依赖取证受阻；任务 2 SQLite/Qdrant 事务链推进。

## 已完成

- [x] WSL Docker 门槛恢复。
  - `docker version`：client/server `29.4.0`，server 可达。
  - `docker compose version`：`v5.1.2`。
  - `docker buildx version`：`v0.33.0-desktop.1`。
- [x] 在 `$PROJECT_ROOT` 初始化 Git，初始分支为 `main`。
- [x] 核对输入：6 个 DOCX，共 `22,358,173` bytes；另有 6 个
  `Zone.Identifier` 文件。详细 SHA256 见
  `design/evidence/task0-baseline.md`。
- [x] 冻结 `docs/` 目录摘要：
  `116e6cd879d4655b43c6ff7833d03a488b8ef15eb0f3970ed24ee77c41a4d15f`。
- [x] 冻结只读参考仓库的 Git 与 tracked-worktree 摘要：
  - HEAD：`03d51db2c0e57ade04c8f9fe035316907d2717f5`。
  - HEAD tree：`84a0a960426da37111a93a806242543c61a881a9`。
  - 182 个跟踪文件工作区摘要：
    `2dbcc7256de517de9c334be18f30e9f6bcd3e71f810d3d9b6b5f62c4e8c5a6e6`。
  - `git status --short --untracked-files=no` 为空。
- [x] 建立 Python 3.11 工程基线。
  - `.venv` 使用 Python `3.11.15`，不使用系统 `3.11.0rc1`。
  - 容器基础镜像固定为
    `python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1`。
  - `pyproject.toml` 固定直接依赖，`requirements.lock` 固定完整版本清单。
- [x] 定义 `Parser`、`Element`、`Locator`、`Chunk`、`IndexManifest`
  契约，并实现第一版安全 `DocxParser`。
  - 红证据：首次 pytest 收集因 `rag_app` 不存在产生 2 errors。
  - 绿证据：契约与安全解析测试 `7 passed in 0.25s`；
    Ruff 全绿；mypy 严格模式 `no issues found`。
  - 文件重命名不改变 doc/chunk ID；定位不使用页码。
  - 拒绝路径穿越、ZIP 解压炸弹和 `Zone.Identifier`。
- [x] 真实解析 6/6 DOCX：22,358,173 bytes、226 标题、579 段落、
  71 表格、132 图片引用。132 次引用指向归档中的 126 个媒体条目，
  OCR 状态将在 `.60` 验证 PP-OCRv5 后按媒体内容去重并回填全部引用。
- [x] 2026-07-27 接续复跑基线：
  - `.venv/bin/python -m pytest -q`：`11 passed in 0.13s`。
  - `.venv/bin/ruff check .`：`All checks passed!`。
  - `.venv/bin/mypy src`：`no issues found in 5 source files`。
  - `git diff --check`：退出 0。
  - 真实输入仍为 6/226/579/71/132/126。
- [x] 固定本项目独立 Qdrant 镜像：
  `qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286`。
  未加载或修改 CovLink 的 v1.15.2 归档。
- [x] 核验 `.58` 两个 LLM 端点，详细证据见
  `design/evidence/task0-dependencies.md`。
  - `:8000/:8001` health 均为 HTTP 200。
  - models 均为 `Qwen/Qwen3-8B-AWQ`，上下文上限 8192。
  - temperature=0、thinking off 的 UTF-8 最小请求均返回 `OK.`。
- [x] `.57:8000/:8001` 的 health 和 models 均在 3 秒连接超时；
  `.60` 内部 embedding/reranker 因无 SSH 身份尚未核验。
- [x] 任务 1 契约升级：
  - manifest 持久 `source_id`，内容版本为 `sha256:<content hash>`。
  - pipeline fingerprint 对版本与参数做规范化 SHA256，manifest 会拒绝错配。
  - Chunk 保存 source/version/fingerprint、原文、embedding 上下文、
    OCR 置信度和前后相邻 chunk ID。
  - 纯重命名由 manifest 保持 source ID；路径只用于展示，不进入 chunk ID。
- [x] 结构切块第一轮静态实验：
  - 标题路径只进入 `embedding_text`，引用 `text` 保持原文。
  - 普通标题/段落/列表不跨结构重叠；表格按表头+行组；长元素才 overlap。
  - Qwen tokenizer SHA256：
    `def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a`。
  - 384/512/64 产生 894 块（226 标题、579 段落、89 表格），
    token p50/p95/max 为 14/240/383。
  - 固定 512 基线产生 70 块，token p50/p95/max 均为 512。
  - 仅完成静态分布；冻结问答集召回消融前不把 384/512/64 定为生产参数。
  - 新评测脚本红证据：首次收集因 `evaluation` 模块不存在报错；
    还原后全套 `20 passed in 0.14s`，Ruff/mypy/diff 全绿。
- [x] 建立 SQLite WAL 单写者状态基线。
  - 任务幂等键唯一；租约到期后可回收；来源版本依次经过
    staging/active/retired/failed，失败不覆盖旧活动版本。
  - 内容哈希唯一时可把纯重命名映射回原 source ID；OCR 结果按媒体摘要与
    OCR revision 幂等缓存。
  - 红证据：首次收集因 `rag_app.state` 不存在产生 1 error。
  - 绿证据：SQLite 专项 `6 passed`。
- [x] 建立 Qdrant v1.18.3 真实容器索引基线。
  - collection 固定 1024 维命名 dense 向量、带 IDF modifier 的 `bm25`
    sparse 向量、pipeline fingerprint 与 payload 索引。
  - staging 点完整后才切 active；激活异常恢复旧 active；失败 staging 可删除。
  - 全量 collection 通过单个 alias update 请求切换；可创建 snapshot。
  - 红证据：首次收集因 `rag_app.index` 不存在产生 1 error。
  - 绿证据：真实 Qdrant 专项 `2 passed in 8.81s`。
- [x] 2026-07-27 SQLite/Qdrant 阶段完整门禁：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - Ruff：`All checks passed!`。
  - mypy：`Success: no issues found in 14 source files`。
  - pytest：`28 passed, 2 warnings in 8.68s`；两条告警均为本地 HTTP
    测试连接携带 API key，测试 Qdrant 仅监听 `127.0.0.1:6333`。
  - `git diff --check`：退出 0。
- [x] 修复跨版本相同 chunk 在 staging 阶段覆盖旧 active 点的问题。
  - 红证据：真实 Qdrant 回归只返回 1/2 个旧版活动 chunk，
    `1 failed in 3.12s`。
  - 物理 point ID 改为 `doc_version + chunk_id`；对外逻辑 chunk ID
    仍跨版本稳定，重复 staging 仍为幂等 upsert。
  - 绿证据：旧版在新版 staging 期间仍完整可查，专项
    `1 passed in 2.83s`。
- [x] 实现单文档跨存储激活与崩溃收敛基线。
  - 顺序固定为：完整 staging 点数校验 → Qdrant 启新停旧 →
    SQLite 记录 active；不会先公布未写完版本。
  - 同一版本重复执行返回 unchanged 且 Qdrant 总点数不增加。
  - 解析/编码失败会清理 staging 并记录错误类型，旧 active 仍可查。
  - 模拟“Qdrant 已激活、SQLite 尚为 staging”后重启，直接确认 SQLite，
    不重复解析或编码；删除重复执行后活动证据与活动来源均为空。
  - 真实 Qdrant + SQLite 专项：`10 passed, 4 warnings in 13.43s`；
    Ruff 与 strict mypy 同轮退出 0。
- [x] 实测 Qdrant collection snapshot 恢复。
  - snapshot 文件名与 Qdrant 返回的 SHA256 均做严格格式校验；
    恢复只允许容器内固定 `/qdrant/snapshots/<collection>/` 路径。
  - 删除物理 collection 后从 snapshot 恢复，活动证据可查询，
    随后重新切换 alias 成功。
  - 真实 Qdrant 专项：`1 passed in 7.00s`。
- [x] 持久化不可变索引 manifest 历史与启动兼容门禁。
  - SQLite 记录规范化 manifest JSON/SHA256、pipeline fingerprint、
    collection、snapshot 文件名/SHA256 与 staging/active/retired 状态。
  - alias 切换前 staged manifest 不替换旧 active；激活使用单个 SQLite
    事务；运行时 collection 或 pipeline 不一致时拒绝启动。
  - 活动 manifest 通过同目录临时文件、fsync 和原子 replace 导出。
  - 红证据：首次收集 `ModuleNotFoundError: rag_app.manifest`。
  - 绿证据：`2 passed in 0.17s`，Ruff 与 strict mypy 退出 0。
- [x] 实现全量索引 snapshot → alias → manifest 发布事务。
  - 首次发布先创建带 checksum 的 snapshot，再用单个 Qdrant 请求切 alias，
    最后在 SQLite 激活 manifest；普通异常会把 alias 回滚到旧 collection。
  - 模拟“alias 已切、manifest 仍 staging”的进程中断，重启后不重新建
    snapshot，直接确认 manifest；再次执行返回 unchanged。
  - 红证据：首次收集无法导入 `FullIndexPublisher`。
  - 绿证据：真实 Qdrant 专项 `1 passed in 7.15s`。
- [x] 2026-07-27 索引恢复与 manifest 阶段完整门禁：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - pytest：`33 passed, 5 warnings in 20.75s`，skipped=0。
  - Ruff：修复 1 个 snapshot SHA 常量检查后 `All checks passed!`。
  - mypy：`Success: no issues found in 17 source files`。
  - `git diff --check`：退出 0。
- [x] 实现确定性目录增量规划。
  - 同路径按摘要区分 unchanged/update；仅“未匹配新旧来源均恰好一个且
    内容摘要相同”时判定 rename；重复内容歧义保守拆成 add/delete。
  - 动作按类型和路径稳定排序，计划生成规范化 SHA256，输入顺序不影响结果。
  - 红证据：首次收集 `ModuleNotFoundError: rag_app.index.planner`。
  - 绿证据：`2 passed in 0.56s`，Ruff 与 strict mypy 退出 0。
- [x] 将增量计划拆为可重入 SQLite 任务项。
  - job 与 plan digest 一对一；同 job 重复保存不重复插入，内容变化拒绝。
  - unchanged 项直接完成；pending 项按序领取；进程中断留下的 running 项
    在 job 租约被回收后可再次领取并增加 attempt。
  - 红证据：首次收集 `ModuleNotFoundError: rag_app.state.plans`。
  - 绿证据：`2 passed in 0.75s`，Ruff 与 strict mypy 退出 0。
- [x] 纯重命名同步 Qdrant 引用 locator 与 SQLite 当前路径。
  - 先逐点幂等更新活动证据的 `source_path` 和所有 locator 文件名，
    再确认 SQLite 路径；进程中断可按同一计划重放。
  - 真实 Qdrant 验证逻辑 chunk ID/点数不变且引用显示新文件名：
    `1 passed in 3.36s`；Ruff 与 strict mypy 退出 0。
- [x] 补齐 job 租约续期与终态所有权门禁。
  - 只有当前 running 租约所有者能续租或结束任务；结束时清除租约，
    根据无/有错误码进入 succeeded/failed。
  - 红证据：新增测试首次因 `renew_job_lease` 不存在失败。
  - 绿证据：状态库专项 `7 passed in 0.54s`，Ruff 与 strict mypy 退出 0。
- [x] 串通单 worker 的全增量动作执行。
  - worker 只领取 pipeline fingerprint 一致的 job；逐项续租并顺序执行；
    单项最多 3 次，失败后继续不受影响项，最终 job 如实标记失败。
  - 真实 Qdrant 场景覆盖初次新增，以及随后重命名、修改、新增、删除；
    对新增注入一次 TimeoutError，第二次成功，任务项 attempt=2。
  - 红证据：首次收集 `ModuleNotFoundError: rag_app.index.worker`。
  - 绿证据：worker/计划专项 `3 passed, 1 warning in 3.78s`；
    Ruff 全绿，mypy `no issues found in 20 source files`。
- [x] 建立中文 BM25 离线候选基线，尚未冻结生产参数。
  - Qdrant 官方文档要求 sparse field 启用 IDF，且 ingest/query 使用相同
    处理选项；非空格语言建议 `multilingual` tokenizer：
    `https://qdrant.tech/documentation/search/text-search/full-text-search/`。
  - v1.18.3 本地容器的 `qdrant/bm25` 可在无外部推理服务时生成 sparse
    vector；显式 `language=none` 避免默认英文词干与停用词。
  - 中文子串真实检索中，multilingual 返回正确文档，word 返回空；
    红证据为 `rag_app.retrieval` 不存在，绿证据为
    `1 passed in 3.21s`。
  - 这只排除明显不适合的 word 基线；最终 tokenizer/BM25 参数仍必须由
    冻结 50+ 题集（含 holdout）确定。
- [x] 2026-07-27 增量 worker 与 BM25 阶段完整门禁：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - pytest：`40 passed, 7 warnings in 29.00s`，skipped=0。
  - Ruff：`All checks passed!`。
  - mypy：`Success: no issues found in 22 source files`。
  - `git diff --check`：退出 0。
- [x] 建立通用外部 HTTP 韧性底座。
  - 每个依赖独立 httpx Client/timeout；策略显式限制总尝试、连续失败阈值、
    熔断冷却和最大并发；仅网络错误及 408/425/429/5xx 可切端点重试。
  - 4xx 明确区分为终态请求错误；响应必须是 JSON；审计结果只含端点、
    重试数和耗时，不含请求体、问题、原文或密钥。
  - 红证据：首次收集 `ModuleNotFoundError: rag_app.clients`。
  - 绿证据：故障转移/熔断专项 `2 passed in 0.08s`，Ruff 与 strict mypy
    退出 0。
- [x] 固化 embedding/reranker 请求与响应 schema。
  - embedding：`/v1/embeddings`，显式 instruction、`truncate=false`、
    float 编码，并按条数与字符双预算分批。
  - reranker：`/rerank`，`query+texts`、`truncate=false`。
  - 两者均校验响应数量、唯一且完整的原索引、有限数值；embedding 另强制
    manifest 维度，reranker 分数强制 `[0,1]`。审计对象不保留输入或向量。
  - 红证据：首次收集 `rag_app.clients.model_services` 不存在。
  - 绿证据：schema 专项 `3 passed in 0.07s`，Ruff 与 strict mypy 退出 0。
  - `.60` 无 SSH，真实 embedding/reranker 请求仍保持 P0，不把 unit
    MockTransport 当最终联调证据。
- [x] 实现非流式缓冲 LLM 客户端并完成真实故障转移。
  - 请求固定 temperature=0、thinking off、`stream=false`；只接受恰好一个
    choice、模型 ID 一致、`finish_reason=stop`、非空内容和自洽 token usage。
  - 客户端返回值明确标为“尚未通过引用校验”，前端不能直接消费。
  - 红证据：首次收集 `rag_app.clients.llm` 不存在。
  - unit 绿证据：`2 passed in 0.08s`，覆盖坏端点切换与截断生成拒绝。
  - 真实四端点顺序 `.57:8000/.57:8001/.58:8000/.58:8001`：
    两次 3 秒连接失败后切到 `.58:8000`，返回 `OK`、retry_count=2、
    prompt/completion/total=`17/2/19`。
- [x] 实现只按名次的多通道 RRF。
  - dense/BM25、原查询/改写查询各自作为独立通道；不直接混合不可比的
    raw score。
  - 同通道重复 chunk、缺失 chunk ID 或同一 chunk 跨通道 payload 漂移
    均立即失败；并列按最佳名次和 chunk ID 稳定排序。
  - 红证据：首次收集 `rag_app.retrieval.fusion` 不存在。
  - 绿证据：`2 passed in 0.63s`，Ruff 与 strict mypy 退出 0。
- [x] 实现有界条件问题改写。
  - 无历史或当前问题无省略/代词信号时不调用 LLM；历史输入只包含用户问题，
    不包含历史答案；原查询始终是首个召回查询。
  - 历史轮数、历史 token、当前问题 token 与输出 token 均有硬上限；
    prompt 将历史标为不可信数据，输出使用严格 JSON Schema。
  - 外部失败、schema 错、同义原句或超预算结果均回退原查询，不阻断检索。
  - 红证据：首次收集 `rag_app.retrieval.rewrite` 不存在。
  - 绿证据：`2 passed in 0.84s`，Ruff 与 strict mypy 退出 0。
- [x] 实现状态、权威级别与有效期的 Qdrant 确定性预过滤。
  - 过滤在 dense/BM25 返回 RRF 前执行；`effective_from <= as_of`、
    `effective_to >= as_of`，缺失边界表示不限制。
  - 真实 Qdrant 构造当前、过期、draft、未验证和无日期 5 条证据，
    仅当前 official 与无日期 official 两条返回。
  - 红证据：首次收集 `rag_app.retrieval.filters` 不存在。
  - 绿证据：`1 passed in 3.19s`；strict mypy 退出 0。
- [x] 串通原查询/改写查询的 dense+BM25 混合召回。
  - 所有查询一次批量 embedding；每个查询分别产生 dense 与 BM25 通道，
    先做 metadata 过滤，再交 RRF。
  - topK、候选上限、RRF 常数和 query instruction 全在显式配置中，
    尚未宣称 `40/40/60/24` 为生产定值。
  - 真实 Qdrant + MockTransport embedding 的结构联调覆盖四个通道并保留
    两个查询；不冒充 `.60` 最终模型联调。
  - 红证据：首次收集 `rag_app.retrieval.hybrid` 不存在。
  - 绿证据：`1 passed in 3.24s`，Ruff 与 strict mypy 退出 0。
- [x] 实现严格 rerank 排序阶段。
  - 候选必须含 `embedding_text`；只将显式 candidate_limit 内候选送模型；
    模型分主排序，RRF 仅用于同分稳定排序，chunk ID 最终打破并列。
  - 配置强制 `0 < final <= max_final <= candidate`；无候选不调用外部服务。
  - 红证据：首次收集 `rag_app.retrieval.rerank` 不存在。
  - 绿证据：`2 passed in 0.80s`，Ruff 与 strict mypy 退出 0。
  - `.60` 真实 reranker 仍受 P0 阻塞，unit 结果不计最终精排指标。
- [x] 实现受 token/条数预算约束的原文证据包。
  - 本次请求内分配 E1… 引用 ID；prompt 仅写原文 `text` 与稳定 locator，
    不把标题 `embedding_text` 当引用原文。
  - 整个 evidence JSON 用模型 tokenizer 计数；单条放不下则完整跳过，
    不截断伪造片段；证据整体标明为不可信数据。
  - OCR 置信度缺失或低于显式阈值时保留 `low_confidence_ocr=true`，
    供逐 claim 门禁使用。
  - 红证据：首次收集 `rag_app.generation` 不存在。
  - 绿证据：`2 passed in 0.54s`，Ruff 与 strict mypy 退出 0。
- [x] 实现逐 claim 引用发布门禁与一次修复。
  - 每条 claim 必须有本次 E-ID、存在于对应原文的逐字 quote；引用 ID、
    quote、重复支持和数字支持均做确定性校验。
  - claim 的全部支持若均为低置信 OCR 则拒绝；显式提示注入 chunk 在进入
    LLM 前隔离，仅剩注入时直接拒答。
  - 首次结构化输出无效时只修复一次；第二次仍无效返回稳定拒答码，
    未校验内容永不进入 AnswerResult.answer。
  - 红证据：首次收集 `rag_app.generation.answer` 不存在。
  - 绿证据：回答/证据专项 `7 passed in 0.79s`；strict mypy 退出 0。
- [x] 2026-07-27 检索、模型客户端与回答门禁阶段完整门禁：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - pytest：`62 passed, 9 warnings in 34.04s`，skipped=0。
  - Ruff：`All checks passed!`。
  - mypy：`Success: no issues found in 34 source files`。
  - `git diff --check`：退出 0。
- [x] 实现 TTL/轮数上限的最小多轮问题存储。
  - SQLite 只存历史用户问题，不存历史答案；过期读取会级联删除，
    每次追加只保留最近 max_rounds。
  - turn_id 支持幂等追加；清空立即删除会话。
  - 红证据：首次收集 `rag_app.state.conversations` 不存在。
  - 绿证据：`1 passed in 0.13s`，Ruff 与 strict mypy 退出 0。
- [x] 建立 live/ready 语义与 LLM 健康策略。
  - `/live` 不依赖外部服务；`/ready` 对全部必需组件做严格 AND。
  - LLM 探针同时要求 `/health` 2xx 和 `/v1/models` 含冻结 model ID，
    但四端点只要求至少一个健康。
  - 红证据：首次收集 `rag_app.api` 不存在。
  - unit 绿证据：`2 passed in 0.20s`；Ruff 与 strict mypy 退出 0。
  - 真实反向验证：只配置 `.57:8000/:8001` 时 healthy=`0/2`，
    `/ready` 返回 503；加入 `.58:8000/:8001` 后 healthy=`2/4`，
    `/ready` 返回 200。
- [x] 串通条件改写、混合召回、精排、证据组装与发布门禁。
  - 编排器仅通过 callback 实时发 trace、阶段、累计耗时和计数；
    问题、历史、证据及未校验回答均不进入阶段事件。
  - 当前用户问题仅在回答完成后幂等追加，历史答案从不进入改写上下文。
  - 红证据：首次收集失败
    `ModuleNotFoundError: No module named 'rag_app.query_service'`。
  - 绿证据：`1 passed in 0.60s`，Ruff 与 strict mypy 退出 0。
- [x] 实现独立鉴权的查询与索引管理 API。
  - query/admin Bearer token 使用常量时间比较且不可互换；请求 schema
    禁止多余字段并限制会话、问题和幂等键长度。
  - chat 使用 NDJSON 实时发布非敏感阶段事件；只有 QueryOutcome 中已通过
    门禁的完整答案/拒答可作为 final，未校验 token 不会流出。
  - 支持清空会话、幂等创建全量/增量任务和读取任务状态；任务接口不暴露
    租约所有者。
  - 红证据：新增契约用例首次运行 `4 failed`，
    `ApiServices.__init__()` 不接受 query 依赖。
  - 绿证据：API/健康专项 `7 passed in 1.22s`；Ruff 退出 0；
    strict mypy 首轮发现闭包内可选 Path 未收窄，已改为局部不可选值，
    待阶段总门禁复核。
- [x] 实现无外部资源的最小静态验收页。
  - 仅含查询令牌、提问、阶段状态、最终回答、引用和清空会话；
    无上传/管理/账号功能，所有内容用 `textContent` 渲染。
  - HTML/CSS/JS 均为本地固定资源，无字体、CDN 或远端脚本；
    API 测试验证必需控件和三项资源可读取。
- [x] 补齐索引运行时 revision 并封堵跨 pipeline 静默复用。
  - `PipelineSpec.index_revision` 进入规范化指纹；Qdrant revision 改变会
    产生不同 fingerprint，manifest 因而可拒绝不兼容启动。
  - 当前来源状态 schema 不允许同一 `source_id+doc_version` 同时绑定两条
    pipeline；若误用同一状态库会在任何更新前拒绝，并要求新 collection
    使用独立 staging 状态库全量构建，避免把旧活动版本当作新向量。
  - 红证据：跨 pipeline 同内容用例首次 `Failed: DID NOT RAISE`。
  - 绿证据：状态/manifest/全量发布专项 `7 passed in 7.32s`，
    Ruff 与 strict mypy 退出 0。
- [x] 实现固定字段的结构化脱敏审计日志。
  - 记录 trace/job ID、阶段累计耗时、pipeline fingerprint、引用 chunk ID、
    外部端点、重试、模型调用数、任务状态和拒答/错误码。
  - 日志 API 不接收问题、历史、证据原文、回答、幂等键、租约身份或密钥；
    外部 URL 会移除用户信息、query 和 fragment。
  - 反向红证据：临时加入泄露哨兵字段后，防泄露用例
    `1 failed`，准确命中 `assert ... not in text`。
  - 还原绿证据：`1 passed in 0.56s`；相关查询/API/检索专项此前
    `8 passed in 3.84s`，Ruff 与 strict mypy 退出 0。
- [x] 建立并冻结 60 题人工核对集，其中 holdout 15 题。
  - 覆盖 ordinary、numeric、table、OCR、cross_chunk、rewrite、
    multiturn、conflict、unanswerable、prompt_injection；含 6 道 OCR
    locator 题，因服务器 GPU/选型阻塞而明确标为 `blocked_gpu_ocr`，
    未伪造 OCR 答案。
  - 调参加载器只返回 45 道 tuning 标签；holdout 公共加载器返回 15 道
    不含 expected 的问题视图。评分依据为人工规则与逐字证据，不接入
    生成模型裁判。
  - 红证据：首次原文核验 `1 failed, 1 passed`，Q046 locator 未命中；
    第二轮 Q054 locator 未命中；修正定位片段后转绿。
  - 绿证据：`2 passed in 0.23s`；独立校验输出
    `{"cases":60,"holdout":15,"ocr_locators":6,"text_evidence":57}`。
  - `dataset.json` 冻结 SHA256：
    `1feb0567256de70dc456d3478b521b906b9f67071b63a113ee0fb7a13009dcb8`。
- [x] 实现不依赖 LLM 裁判的冻结集质量门槛。
  - 程序确定性计算 Recall@20、rerank Recall@5、MRR、nDCG@20、
    引用 precision/recall、无效引用、可答误拒、不可答拒答与阶段 p50/p95。
  - 正确、完整、事实支持必须填写人工 reviewer；缺题、缺人工评分、未知/
    重复题号或任一阈值不达标均非零退出，6 道阻塞 OCR 题显式排除而非
    伪造通过。
  - 反向红证据：临时把 10 题来源改为 `wrong.docx`，评测退出 1，
    `recall_at_20=0.8 < 0.9`、`rerank_recall_at_5=0.8 < 0.85`。
  - 还原绿证据：合成契约自检退出 0，Recall/rerank/citation/人工指标均
    为 1.0；专项 `5 passed in 0.22s`，Ruff 与 strict mypy 退出 0。
  - 上述合成结果仅验证评测器，不冒充真实模型指标；临时文件已删除。
- [x] 2026-07-27 API、日志与冻结评测阶段完整门禁：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - pytest：`78 passed, 10 warnings in 34.11s`，skipped=0。
  - Ruff：`All checks passed!`。
  - mypy：`Success: no issues found in 46 source files`。
  - `git diff --check`：退出 0。
  - warnings 仅为 Starlette TestClient 弃用提示及本机 HTTP Qdrant
    测试 API key 提示，不含失败或跳过。
- [x] 真实 6 份 DOCX 的本地无 OCR 构建链已闭合。
  - 递归发现严格限制在 `docs/`，拒绝符号链接和越界路径，忽略
    `Zone.Identifier`；6 个输入总计 22,358,173 bytes。
  - 使用真实 `DocxParser`、本地 embedding tokenizer 和 384/512/64
    临时参数完成解析与切块，共 894 个正文/表格 chunk；测试替身只验证
    embedding 批次顺序与维度，不作为真实模型指标。
  - 132 个图片引用按媒体 SHA256 去重为 126 个状态，全部记录为
    `pending/GPU_OCR_PENDING_SELECTION`，本地未下载、选择或运行 OCR，
    且 OCR chunk 数为 0。
  - 红证据：专项首次收集报
    `ModuleNotFoundError: No module named 'rag_app.index.build'`。
  - 绿证据：专项 `2 passed in 2.12s`；Ruff `All checks passed!`；
    strict mypy `Success: no issues found in 3 source files`。
- [x] 管理任务已接通单索引 worker 的全量/增量执行闭环。
  - API 与 CLI 共用 WAL 控制任务表；`rag-worker` 独立容器串行领取任务，
    每个物理 collection 使用独立 SQLite 状态库和持久同步计划，进程中断后
    依靠任务租约、稳定计划摘要和 staging 点数恢复。
  - 全量任务使用确定性新 collection，完整成功后创建 snapshot，再切换 alias
    并激活 manifest；增量任务覆盖新增、修改、删除和唯一重命名，活动
    collection 的每次完整来源清单和 snapshot 作为不可变 revision 追加保存。
  - `rag-app index {full|incremental} --idempotency-key ...` 可同步执行；
    `rag-app worker` 为部署入口。检索配置未冻结或 embedding/reranker/LLM/chunker
    revision 含 pending/unknown/provisional 时拒绝写索引。
  - 红证据：闭环专项首次收集报
    `ModuleNotFoundError: No module named 'rag_app.index.job_runner'`。
  - 绿证据：真实 Qdrant 全量→增量专项连同 manifest/state 回归
    `14 passed, 2 warnings in 13.49s`；worker/deployment 专项
    `4 passed, 1 warning in 6.29s`；Ruff 与 strict mypy 退出 0；
    `docker compose config -q` 退出 0。
- [x] 实现重排后有界相邻块扩展。
  - 保留全部 top rerank 命中后，仅在 `max_final_limit` 尚有余量时按命中顺序
    补充前/后块；读取时强制 `version_state=active`，并再次校验相同
    `source_id+doc_version`，禁止跨文档或跨版本拼接。
  - Qdrant 为 `chunk_id` 建 payload 索引，扩展后的条数进入阶段事件；
    最终仍由证据 token 预算和引用校验门禁裁决。
  - 红证据：专项首次收集报
    `ModuleNotFoundError: No module named 'rag_app.retrieval.neighbors'`。
  - 绿证据：真实 Qdrant 相邻扩展及索引/查询回归
    `6 passed, 5 warnings in 19.59s`；Ruff 退出 0；strict mypy
    `Success: no issues found in 6 source files`。
- [x] 完成 10 万 synthetic chunk 的真实 Qdrant 检索容量验收。
  - 独立脚本构建 100,000 点、1024 维 named dense collection，payload
    使用活动版本过滤；写入完成并等待 collection green 后预热，再执行
    200 次 top20 查询，结束后删除基准 collection。
  - 输出：`elapsed_seconds=17.863`、`p50_ms=3.221`、
    `p95_ms=3.625`、`max_ms=3.824`，满足 p95≤500ms。
  - 完整机器可读证据位于
    `evaluation/results/qdrant-100k.json`；脚本 Ruff、strict mypy、
    compileall 与 1,000 点冒烟均退出 0。
- [x] 建立 5 并发 30 分钟 chat 负载验收脚本。
  - 固定默认 `concurrency=5`、`duration_seconds=1800`，逐请求要求 HTTP 200
    且最后一条 NDJSON 为 `final`，计算错误率与验证后答案 p95，阈值分别为
    `<1%` 与 `≤60s`。
  - 当前真实运行由 `.60` 模型核验和 frozen 配置阻塞，未用 mock 或本地模型
    伪造端到端性能结果；阻塞已同步写入 `BLOCKED.md`。
- [x] 生成并校验当前源码对应的 linux/amd64 离线交付候选。
  - 当前应用 wheel SHA256：
    `01929c1d987a992841c9373a6ccdf93a5ef84d112e3869c013c6a4216e1fdfd3`。
  - `docker buildx build --network none --platform linux/amd64 --load`
    成功；应用镜像
    `sha256:f85c569e06fcbbe423b9fe72097afdfe326465a377b00a1b7fa207d92b2f5dc3`，
    `linux/amd64`，166,625,253 bytes。
  - 容器自检先因本轮配置/前端变更与旧 `ASSETS.sha256` 不一致而非零，更新 3 个实际变更资源
    的摘要并重建后，`docker run --rm --network none ... asset-selfcheck` 退出 0，报告
    7 个固定资源、tokenizer probe=11；镜像 CLI 含
    `serve/worker/index/asset-selfcheck`。
  - 离线包：
    `artifacts/rag-docx-offline-0.1.0-linux-amd64-20260727T055740Z.tar`，
    316,395,520 bytes，SHA256
    `054a8419ed0ee8e1dc932a301264cd5027ddb0fab6ab2435cb007c89c7582041`。
  - 包内主 manifest 共 63 项，并纳入冻结集自身 manifest；包含 6 DOCX、
    39 wheels、应用/Qdrant 镜像、两份 CycloneDX SBOM、Compose、配置及
    部署/回滚/离线校验脚本。`verify-offline.sh` 三层 SHA256 校验退出 0。
- [x] 2026-07-27 第 2 轮本地完整门禁：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - Ruff：`All checks passed!`。
  - mypy：`Success: no issues found in 48 source files`。
  - pytest：`90 passed, 12 warnings in 46.93s`，skipped=0。
  - warnings 仅为 TestClient 弃用提示与本机 HTTP Qdrant 测试 key 提示。
  - `bash -n deployment/*.sh`、`docker compose config -q`、
    `git diff --check` 均独立退出 0。

- [x] 补齐第二轮本地验收缺口：逐图片引用、分档召回、OCR CER、反馈统计和 30 分钟负载契约。
  - 红证据：专项首次运行 `4 failed, 2 passed`；分别暴露
    `duration_seconds=600`、缺少 `recall_at_5`/`ocr_cer`、
    `StateStore.count_media_references` 不存在。
  - 绿证据：专项 `6 passed in 2.60s`；真实 6 DOCX 记录 132 条图片引用状态、
    126 个唯一媒体 OCR 缓存，其中 `image/emf` 引用 18 条，全部仍为
    `pending/GPU_OCR_PENDING_SELECTION`，未在本地运行 OCR。
  - 评测报告新增 Recall@5/10/20、OCR CER 及实际评测字符数、用户有用/没用反馈数与有用率；
    完成门槛同步为 Recall@20≥95%、rerank Recall@5≥90%。
  - 负载脚本默认改为 5 用户持续 1800 秒；真实运行仍等待 frozen 配置和 `.60` 联调。
  - strict mypy：`Success: no issues found in 10 source files`；Ruff 首次只发现一条本次新增的
    无效 `noqa`，已删除，待完整门禁复核。

- [x] 建立用户“有用/没用”反馈的非敏感闭环。
  - 只持久化 32 位 `trace_id`、布尔有用性和 UTC 时间；不保存问题、答案、引用原文或令牌。
  - 同一 trace 重复提交为幂等更新；`POST /api/feedback` 只接受查询令牌，管理令牌不可互换。
  - 最小静态页在最终答案发布后启用“有用/没用”，提交成功后立即禁用，未增加管理 UI。
  - 红证据：专项首次收集因 `rag_app.state.feedback` 不存在而 `2 errors`。
  - 绿证据：反馈存储、鉴权 API、静态页与健康回归 `9 passed in 1.29s`；
    Ruff `All checks passed!`；strict mypy `Success: no issues found in 6 source files`。

- [x] 实现可配置软路由及低置信全库回退。
  - 路由规则只引用稳定 `source_id`；唯一最高关键词覆盖率达到冻结阈值时才追加 Qdrant
    来源过滤，低分、并列、无规则均使用空来源过滤并检索全库。
  - 阶段事件记录 route ID、千分置信度和 fallback，不含查询文本；混合召回依赖仍通过窄职责
    `HybridRetrievalServices` 注入，未绑定具体路由模型。
  - `deployment/config/retrieval.json` 显式保留 `soft_route_min_confidence=0.75` 和空规则，
    且整体状态仍为 `provisional`；阈值与实际规则必须经冻结集确定前不会 ready 或写索引。
  - 红证据：专项首次收集报
    `ModuleNotFoundError: No module named 'rag_app.retrieval.routing'`。
  - 绿证据：软路由、真实 Qdrant 混合召回/元数据过滤、查询链和配置回归
    `8 passed in 6.46s`；Ruff `All checks passed!`；
    strict mypy `Success: no issues found in 12 source files`。

- [x] 2026-07-27 第 3 轮本地完整门禁与离线候选复核：
  - `compileall -q src tests scripts evaluation`：退出 0。
  - Ruff：`All checks passed!`。
  - 仓库配置下 strict mypy：`Success: no issues found in 50 source files`。
  - pytest：`96 passed, 12 warnings in 47.98s`，skipped=0；warnings 仍仅为
    TestClient 弃用提示和本机 HTTP Qdrant 测试 key 提示。
  - `bash -n deployment/*.sh`、`docker compose config -q`、
    `git diff --check` 与部署专项 `8 passed` 均独立退出 0。
  - 新候选包主 manifest 63 项、wheel manifest 39 项、DOCX manifest 6 项；
    包内 `verify-offline.sh` 三层 SHA256 校验退出 0。
  - Docker SBOM 生成器报告部分二进制 buildinfo/relationship 解析 warning，但命令退出 0，
    两份 CycloneDX 文件已进入主 manifest 并通过摘要校验。
  - Git 仍为 `No commits yet on main`，未 commit、未 push。

- [x] 收紧完成条件对应的评测硬阈值与引用原文校验。
  - 发现旧实现仅检查引用 ID，且未把可答误拒、引用 precision/recall、提示注入通过率纳入硬阈值；
    OCR CER 的分子/分母也允许只提供一项。
  - 红证据：新增回归测试首次运行 `3 failed, 3 passed`；错误引用原文仍被计为命中，
    `EvaluationThresholds` 缺少可答误拒阈值，不完整 OCR CER 未拒绝。
  - 修正后硬阈值为 Recall@20≥95%、rerank Recall@5≥90%、可答误拒≤10%、
    不可答拒答≥95%、引用 precision/recall/事实支撑=100%、提示注入防护=100%；
    引用命中同时校验 chunk ID 与冻结原文，OCR CER 两项必须成对提供。
  - 绿证据：评测专项 `6 passed in 0.08s`；Ruff `All checks passed!`；
    strict mypy `Success: no issues found in 2 source files`。
  - 第 3 轮内完成修正后的全量复核：`compileall` 退出 0，Ruff 全绿，
    strict mypy `Success: no issues found in 50 source files`，
    pytest `99 passed, 12 warnings in 51.73s`、skipped=0；
    `bash -n deployment/*.sh`、`docker compose -f deployment/compose.yaml config -q`
    与 `git diff --check` 均退出 0。
  - 本次仅修改 `evaluation/` 与测试/进度证据，不改变 `src/`、运行时 wheel、
    镜像或离线包内容；因此现有离线候选包摘要保持有效。
  - 该历史轮次结束时尚无 commit；当前目标随后明确要求本地分组提交，
    所以上方新目标记录取代旧状态。全过程仍未配置 remote、未 push。

## 当前交付边界与未完成项

- [x] 当前目标固定 OCR 为 PaddleOCR 3.5.0、PP-OCRv5 server det/rec、
  `paddle_static`，并已交付代码、CPU 真测证据、固定资产来源和离线部署资料。
- [x] 主应用保持 Python 3.11；OCR 容器基于固定 digest 的 PaddlePaddle
  Python 3.10 GPU 运行时，使用独立 CPython 3.10 wheel manifest。
- [x] `docs/**`、既有 artifacts/frozen/results/evidence/验收报告及参考仓库
  全程只读，终检 SHA 未漂移。
- [ ] GPU 镜像构建、断网自检、目标服务器部署与真实性能由用户执行；
  本轮按边界未运行 `docker build/save`，也未 SSH `.57/.58/.60`。
- [ ] 18 个 EMF 在固定转换器及许可证完成审计前保持
  `EMF_RASTERIZER_UNAVAILABLE`，不冒充 OCR 成功。
- [ ] 生产 embedding/reranker/LLM revision 与检索参数仍需目标环境和人工
  冻结集证据；当前 provisional 配置不能 ready 或计为最终验收。
- [ ] GitHub refs 在线复核因 60 秒超时未完成；本地仓库无 remote、push=0。

具体解除条件和待用户回填证据见 `BLOCKED.md`。

## 2026-07-28 任务 3：严格配置与元数据契约

- [x] 先补 pipeline/retrieval/corpus policy 重复 JSON key、schema version、
  重复参数/model key、空 revision、非法状态/权威级别、数字及非 RFC3339
  时间、倒置时间、元数据省略和空过滤集合反测；首次专项为
  `22 failed`，证明旧实现会覆盖重复 key、接受宽松日期和隐式元数据。
- [x] 三类配置已统一经 `strict_json.load_json_file` 加载，任意层级重复 key
  均在 Pydantic 及外部状态前 fail closed；错误不含重复字段名、配置值或路径。
- [x] pipeline schema 固定为 `2`；schema、parser、元数据词表、OCR、chunker、
  embedding、sparse、index revision 与 corpus policy 语义摘要均进入规范化
  index fingerprint。chunker 恰含三个唯一既有 key，LLM model key 唯一，
  除 document embedding instruction 外所有 model/revision/index 字段非空。
- [x] 状态词表固定为 `active/draft/retired`，权威级别固定为
  `official/verified/unverified`；日期仅接受带 `T` 和 `Z`/偏移的 RFC3339
  字符串或 null。Chunk/Chunker 不再合成元数据，corpus policy 是唯一来源。
- [x] 首次全量回归暴露旧测试构造缺少新必填元数据：
  `20 failed, 201 passed, 22 warnings`；只补显式合法 fixture 后专项
  `56 passed`，相关 strict mypy 为 `83 source files` 无问题。
- [x] 配置交叉核验输出
  `policy_digest_matches_pipeline=True`、`pipeline_schema=2`；探针首次误用
  不存在的 `CorpusPolicy.sha256()` 退出 1，改用公开
  `semantic_sha256()` 后退出 0。`deployment/ASSETS.sha256` 8 项全部 `OK`。
- [x] 阶段完整门禁：compileall 退出 0；Ruff `All checks passed!`；mypy
  `Success: no issues found in 79 source files`；pytest
  `222 passed, 22 warnings in 83.03s`、skipped=0；默认 docstring
  `missing_google_sections=0`；四个 shell 和 `git diff --check` 均退出 0。
- [x] Compose 在未加载必填环境变量时按预期退出 1；显式使用
  `deployment/.env.example` 后 `docker compose ... config -q` 退出 0。

## 2026-07-28 任务 4：OCR 请求级校验与端点故障转移

- [x] 先新增坏 SHA、坏 revision、缺字段和全端点坏反测；首次定向执行
  `4 failed, 6 deselected, 1 warning`，坏响应只调用首端点并在客户端末端抛错，
  证明校验发生得过晚。
- [x] `OcrClient.recognize()` 现为每次请求向 `request_json()` 传入闭包，
  在当前 endpoint attempt 内同时校验完整 `OcrResponse`、本次媒体 SHA 和
  revision；worker 的 revision-only pool validator 及其冗余依赖已删除。
- [x] 错 SHA、错 revision、非法/缺失 schema、非有限 confidence/bbox 均作为
  当前端点失败切换下一端点；全部端点无效时稳定抛
  `ExternalServiceUnavailableError: INVALID_RESPONSE_SCHEMA`，异常不含响应正文
  或媒体摘要。
- [x] builder 回归证明 `OCR_SERVICE_UNAVAILABLE` 不形成永久命中，同一媒体与
  revision 在服务恢复后重试并把旧失败覆盖为 succeeded；成功结果继续复用，
  `OCR_REQUEST_REJECTED` 仍是终态缓存且不会重复请求。
- [x] 专项回归 `35 passed, 2 warnings in 2.06s`；阶段完整门禁为 compileall
  退出 0、Ruff 全绿、mypy `79 source files` 无问题、pytest
  `228 passed, 22 warnings in 85.57s`、skipped=0；默认及 `--changed`
  docstring 均为 `missing_google_sections=0`，`git diff --check` 退出 0。

## 2026-07-28 任务 5：有界查询执行与可恢复 runtime 构造

- [x] 执行器红测首次因 `rag_app.query_executor` 不存在而收集失败；新增进程级
  `QueryExecutor`，固定 max_workers=4、max_queue=8、总容量 12，生产排队上限
  60 秒，稳定 `Retry-After=5`，关闭后拒绝新任务。
- [x] 并发反测证明第 5 个查询排队且活动峰值始终为 4，12 个可准入，第 13 个
  在 0.1 秒内拒绝；排队超时会取消未开始任务，异常后容量恢复，固定 4 个
  `rag-query-worker` 线程在 close 后全部 join。执行器专项 `6 passed`。
- [x] API/流红测夹具首次因构造失败后的测试清理缺口超时；修正测试 finally 后
  得到真实 `3 failed`：ApiServices 和 stream 尚无执行器准入。修复后
  `/api/chat` 在构造 StreamingResponse 前完成准入，满载/超时均返回 HTTP 429、
  稳定 Retry-After 且不暴露线程、队列或端点信息。
- [x] `stream_query` 不再逐请求 `threading.Thread(...).start()`；阶段队列保持
  有界，流关闭后停止写消息，查询完成再释放容量。success、refusal、异常、
  generator close 与客户端断开使用同一 finally/worker 收敛路径；排队任务
  不会提前进入模型客户端或占用模型 semaphore。
- [x] runtime/worker close 红测首次 `2 failed`，暴露 RuntimeBundle 无 executor
  所有权且 worker 重复关闭/顺序错误；现已幂等，并按 executor→readiness→HTTP
  →Qdrant、worker 资源构造逆序关闭。活动查询未结束时 network close 调用为 0。
- [x] `ExitStack` 已覆盖 Qdrant、部分/完整 HTTP 客户端、readiness、executor
  和 OCR HTTP 所有权；`create_app`、`readiness.start`、第三个 HTTP client
  构造及 worker StateStore.initialize 注入失败均逆序关闭，专项
  `21 passed`。成功返回后所有权仅属于 RuntimeBundle/WorkerRuntimeBundle。
- [x] embedding/reranker/LLM 默认并发及 `.env.example`/Compose 显式默认值均
  改为 4，OCR 保持 1；旧冻结断言产生 `1 failed` 后更新为 4 并转绿。
  这些运行参数未进入 index 或 serving semantic fingerprint。
- [x] 任务 5 合集 `40 passed, 2 warnings`；阶段全量门禁为 compileall 退出 0、
  Ruff 全绿、mypy `80 source files` 无问题、pytest
  `243 passed, 22 warnings in 85.82s`、skipped=0。默认及 `--changed`
  docstring 均为 0，四个 shell、Compose、8 项资产 SHA 和
  `git diff --check` 均退出 0；递归搜索仅 readiness 保留一个显式
  `threading.Thread`，查询路径只使用固定 ThreadPoolExecutor。

## 2026-07-28 最终本地交付审计

- [x] 为完成条件 9 复核实际 build context 时，发现 OCR 的源码重新纳入规则位于
  缓存排除之后，可能重新带入本轮门禁生成的缓存；两个
  `Dockerfile.dockerignore` 均改为在全部 `!` 规则之后再次排除
  `**/__pycache__/`、`**/*.pyc`、`**/*.pyo`。契约测试
  `tests/test_ocr_isolation.py` 为 `3 passed in 0.07s`；首次 Ruff 精确报
  `D413` 一项，补一处 docstring 空行后完整 Ruff 转绿。
- [x] 最终核心门禁：compileall 退出 0；Ruff `All checks passed!`；strict mypy
  `Success: no issues found in 80 source files`；全量 pytest
  `244 passed, 22 warnings in 90.24s`、skipped=0，warning 类别仍仅为既有
  Starlette TestClient 弃用提示和本地 HTTP Qdrant API key 提示。
- [x] 默认全仓与显式 `--changed` docstring 检查均输出
  `missing_google_sections=0`。任务 1 已记录的 30 个 docstring-only 文件
  去除 docstring 后仍为 `ast_mismatches=0`。
- [x] 真实 DOCX 最终只读审计退出 0：
  documents=6、bytes=22358173、headings=226、paragraphs=579、tables=71、
  image_references=132、unique_media=126、blank_text_elements=0、
  toc_controls_skipped=3、ordinary_controls_parsed=0、
  unsupported_nodes=15、unsupported_content_with_evidence=0；未输出文件名、
  正文、OCR 文本或证据片段。
- [x] `bash -n deployment/*.sh`、使用 `deployment/.env.example` 的
  Compose config、`git diff --check` 均退出 0。应用资产 8/8、OCR models
  6/6、OCR wheels 59/59、OCR 总清单 69/69、冻结集 1/1 均逐项 `OK`。
  应用资产第一次因错误地从 `deployment/` 解释仓库根相对路径而退出 1；
  回到仓库根执行同一清单后 8/8 转绿，资产未被修改。
- [x] 临时 Git index 模拟 `git add -A` 后 release-safety 输出
  tracked_files=181、binary/large/local-path/private-network/private-path/
  secret/总 violations 全为 0；候选缓存文件为 0。真实 index 前后 SHA256
  均为 `5babd6f4638dbf36aec00991569b3dd240f5e7bfb1fbc1d0eeba5541a6508cbf`。
- [x] 最终本地边界：HEAD 仍为
  `dd997ad517b6b49c2f1a22429e84d35b6ed8d835`，staged=0；未 commit、未 push、
  未联网、未 build/save/package、未访问服务器。`docs`、frozen、results、
  evidence、既有验收文件和参考仓库摘要未漂移；`artifacts` 保持任务 0 发现的
  只读当前值 `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`，
  与旧冻结值不一致的阻塞仍置顶保留，未修改或恢复该目录。

## 2026-07-29 section-aware chunking v2 任务 0：事实基线

- Git 基线：HEAD
  `4fe7b26164e6ad1ee6b1f8477beed0473f7d49fe`，tracked=181、
  untracked=0、staged=0、`git status --short` 为空；本地 `main` 已跟踪
  `origin/main`。恢复检查中的一次 `git ls-remote origin` 已违反本轮禁网边界，
  原始命令、退出码和远端摘要已置顶写入 `BLOCKED.md`，后续不再联网。
- 受保护摘要：`docs`
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  `artifacts`
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`；
  验收文件 `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`。
- 只读参考仓库仍为 HEAD
  `03d51db2c0e57ade04c8f9fe035316907d2717f5`、tree
  `84a0a960426da37111a93a806242543c61a881a9`、tracked 聚合
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  status 为空。应用 8/8、OCR 总清单 69/69、冻结集 1/1 资产均逐项 `OK`。
- 静态基线均退出 0：compileall、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 80 source files`、严格 docstring
  `missing_google_sections=0`、shell syntax、Compose config、
  `git diff --check`。临时 index release-safety 为 tracked_files=181、
  violations=0，真实 index 前后 SHA256 均为
  `2ed436d9ced8664f95d387553a9a71fcb26f23a9b2b30bcc4011a333991dc762`。
- 全量 pytest 首次因 Docker daemon 未运行而为
  `20 failed, 224 passed, 22 warnings`；启动已安装 Docker 后确认本地已有
  `qdrant/qdrant:v1.18.3` amd64 镜像，以 `--pull never` 临时启动。第二次因
  测试 key 配错仍为 20 failed；按夹具固定 key 重启后第三次为
  `244 passed, 22 warnings in 88.43s`、skipped=0，未改测试或拉取镜像。
- 真实 DOCX 只读审计退出 0：documents=6、headings=226、paragraphs=579、
  tables=71、image_references=132、unique_media=126、
  blank_text_elements=0、toc_controls_skipped=3、
  ordinary_controls_parsed=0、unsupported_nodes=15、
  unsupported_content_with_evidence=0。
- `evaluation/chunking_experiment.py` 直接运行先因无法导入 `rag_app` 退出 1；
  显式 `PYTHONPATH=src` 后仍退出 1，严格暴露 `schema_version='1'` 与空
  `llm_model`。源码同时证实 fixed_512 只用总 token 除法、Chunker 调用缺少
  metadata、标题仍生成 chunk、长元素可按字符切分。
- 调用点基线：`stable_chunk_id` 生产定义/调用位于 contracts、chunking 和
  active_evidence；首 locator 生产读取位于 active_evidence、Qdrant payload
  和 AnswerGenerator；previous/next 位于 contracts、chunking、Qdrant 和
  NeighborExpander。`TrustedActiveEvidence`/`_TRUST_MARKER`/公开 verifier
  分布于 active_evidence、evaluation 与 load test；evaluation active_state
  第 66 行会对 `ManifestRepository` 调用 `initialize()`。

## 2026-07-29 本目标顺序与风险（8 行）

1. 先关闭活动证据的伪可信对象和 SQLite 写入入口，再动 chunk 持久契约。
2. 用现场 alias、只读 ACTIVE manifest 和双重前后快照建立唯一生产评分根。
3. 把 element-level 行为冻结为 evaluation-only legacy，并修好真实 fixed baseline。
4. 先用 source span/section/run 的红测锁定契约，再实现确定性 section pack。
5. 表格、OCR 与长原子内容分别切分，普通块之间绝不做统一 overlap。
6. Qdrant payload、邻居扩展、引用 locator 和 audit schema 同步升级到 v2。
7. 最后跑 legacy 与三个 v2 候选的真实结构消融，不查看 holdout 标签。
8. 最大风险是重复表头 span、quote 多次出现映射和现场扫描中途状态漂移。

## 2026-07-29 任务 1：关闭活动证据可信根

- [x] 删除 `TrustedActiveEvidence`、`_TRUST_MARKER` 和公开 verifier；生产
  evaluator/load test 不再接收 `--active-evidence-input`、audit JSON 或外部构造对象，
  而是在同一进程从 operator 指定 alias、现有 SQLite ACTIVE manifest 和 alias 实际
  collection 直接生成现场快照。
- [x] 新增 `ReadOnlyManifestRepository`：SQLite URI `mode=ro`、
  `PRAGMA query_only=ON`，不 mkdir、initialize、执行 DDL、设置 WAL 或启动写事务；
  缺失数据库、不完整 schema 和无效行均 fail closed。反测确认缺失路径不产生数据库，
  完整/不完整 schema 的只读查询均不产生 `-wal`/`-shm`。
- [x] 现场扫描前后重新读取 alias target、ACTIVE manifest/state/digest、collection
  metadata、pipeline fingerprint 和 exact active count；分页结果还逐点重算来源版本、
  locator、text SHA 与 stable chunk ID，任何漂移或计数不一致均拒绝。
- [x] 红证据：首轮新增可信根用例为 `4 failed`，分别命中公开可信包装、只读数据库
  创建、只读 repository 缺失和 metrics 回灌入口。补充 WAL 用例首轮因测试夹具自身使用
  可写 WAL repository 而 `1 failed, 28 passed`，改为 DELETE-journal 最小完整 schema
  后再验证产品只读入口，未放宽断言。
- [x] 绿证据：真实 Qdrant 分页/篡改/旧 collection/旧 pipeline/retired point 以及扫描
  期间 alias 切换、ACTIVE manifest revision 变化、第二次 exact count 变化合计
  `29 passed, 12 warnings in 39.48s`；skipped=0。相关 Ruff 输出
  `All checks passed!`，strict mypy 输出 `Success: no issues found in 6 source files`。

## 2026-07-29 任务 2：真实 chunk 实验与 legacy baseline

- [x] 红证据：`tests/test_chunking_experiment.py` 首轮收集因
  `ModuleNotFoundError: evaluation.legacy_chunking` 退出 1；新增真实 fixed-window、
  来源字符范围和 legacy 标题独立成块断言后，旧摘要又因缺少 p90 为
  `1 failed, 2 passed`。
- [x] 新增 evaluation-only `legacy_chunking.py`，冻结旧版 element-level 标题成块、
  元素内任意字符 target 切分、普通 overlap 和旧表格打包行为；production Chunker
  后续不再承担该兼容逻辑。
- [x] fixed 512 baseline 现在实际拼接证据流并生成有文本、ordered locators 和
  `[source_start_char, source_end_char)` 的无 overlap 窗口，不再用总 token 除法估算。
- [x] `chunking_experiment.py` 改为从 operator 指定的 pipeline、corpus policy 和
  tokenizer 加载 schema v2 真实配置，为每个 source 显式 resolve
  `DocumentMetadata`；输出仅含聚合计数、摘要和 fingerprint，不含私有文件名、标题、
  正文、问题或 quote，并同时统计 citation/embedding token。
- [x] 真实 6 DOCX 命令退出 0：documents=6，legacy/当前结构候选各 894 chunks，
  fixed 512 真实窗口 70 个、p50/p90/p95/max 均 512，tokenizer SHA 与 pipeline
  一致；该结果仅是 provisional structural baseline，不是检索效果或定参结论。
- [x] 专项测试 `3 passed in 1.21s`；Ruff 已全绿，strict mypy 首轮只发现 direct
  script 双分支 import 重定义，改为 direct-script 根路径引导的唯一 typed import，
  待任务 3 合并门禁复核。

## 2026-07-29 任务 3：section-aware chunking v2

- [x] 红证据：新增 section/run/source-span 契约测试首轮收集为 `2 errors`，均因
  `ChunkRole` 不存在；实现初版后为 `6 failed, 6 passed`，精确命中相邻 pair 渲染错误，
  未删除或放宽断言。
- [x] 新增严格 `ChunkRole(TEXT/TABLE/OCR)`、`ChunkSourceSpan` 与 `ChunkIdentity`；
  Chunk 强制保存 section/group/role/spans，span 的 chunk/source 半开区间必须非空、
  等长、有序、不重叠且位于 `Chunk.text` 内，`locators` 必须等于 span locator
  有序去重结果。
- [x] `stable_chunk_id` 现基于 source、section、neighbor group、role、全部 ordered
  span 的 element ID/locator logical key/字符范围/重复标记和完整文本；文件纯重命名
  ID 不变，只改第二个 locator 或后续 span 会改变 ID。
- [x] 标题只开启 section；首标题前正文进入 root；heading_index 区分同名标题；
  空父 section 不成块。连续段落/列表形成 TEXT run，表格和成功/低置信 OCR 图片各自
  独立 run，pending/failed OCR 不成证据且仍终止相邻 TEXT run。
- [x] 短正文只在完整原子边界用确定性 target-nearest 规则打包，段落间 `\n\n`、
  连续列表项间 `\n`；标题路径只进入 embedding text。普通块之间无 overlap。
  长原子依双换行、换行、句号、分号、逗号、空白、hard cut 优先级切分，仅完整句/行
  后缀可在同一长原子内 overlap，找不到则零 overlap。
- [x] 每张表独立并在 segment 重复首行候选表头，普通数据行只在完整行间切分；超长行
  先用单元格边界再用语义边界。每张 OCR 图片独立并优先按原始行打包。previous/next
  只在同一 neighbor group 内连接。
- [x] 专项绿证据：新契约 `12 passed in 0.10s`；合并旧 Chunker、DocxParser 和
  DocxBuild 回归为 `42 passed in 2.02s`；相关 Ruff 全绿，strict mypy
  `Success: no issues found in 4 source files`。
- [x] pipeline revision 更新为 `section-pack-v2-provisional`，真实 6 DOCX 结构运行
  退出 0：v2=236 chunks（paragraph=155、table=81），text p50/p90/p95/max=
  110/339/391/455，embedding=127/346/400/469；legacy=894。参数仍为候选，未冻结。
  `ASSETS.sha256` 更新后 8/8 `OK`。
- [x] 自动编号只读审计：detected=268、markers_not_represented=268；未猜测/伪造编号，
  已按任务书置顶新增 `BLOCKED.md` P1。

## 2026-07-29 任务 4：索引、引用与活动证据 schema v2

- [x] 红证据：Qdrant payload、neighbor group、source span 引用用例首轮为
  `10 failed, 9 passed`，分别命中 collection metadata 缺
  `payload_schema_version`、rename 未同步 span、NeighborExpander 可跨 group、
  EvidenceItem 不保存 span、quote 固定取首 locator 和跨 span 未拒绝。
- [x] Qdrant 新 collection 写入并严格验证 `payload_schema_version=2`，payload
  保存 section/group/role/全部 canonical source spans；rename 同时更新 locators
  与每个 span 内的 locator。runtime 绑定 alias 时、evaluator 现场读取时、worker
  构建模型请求前均拒绝旧/缺失 schema；真实旧 collection 反测通过。
- [x] NeighborExpander 对 seed 和 neighbor 的 source_id、doc_version、
  neighbor_group_id 全部 fail closed；不同 group 不扩展，缺任一字段直接拒绝。
- [x] EvidenceAssembler 只在进程内保存并验证 source spans，不改变 prompt payload。
  AnswerGenerator 枚举 quote 全部出现位置：同 locator 可接受，不同 locator 以
  `AMBIGUOUS_QUOTE_LOCATION` 进入唯一一次修复，跨 span 拒绝，最终返回实际 locator；
  回答 Prompt、JSON Schema、API 和前端协议均未变化。
- [x] audit manifest 升级 v2，保存 locators、section/group/role 与全部 source spans；
  现场重算 text SHA、span 顺序/范围、stable ID、source/version、pipeline、active
  state。第二 locator、后续 span、source range 和身份字段逐项篡改均被真实 Qdrant
  测试检测。
- [x] 绿证据：首组转为 `19 passed, 6 warnings`；audit/evaluation 第二轮为
  `1 failed, 25 passed`（测试尝试删除 Qdrant metadata，但 update API 是 merge），
  改为显式旧版本 metadata 后单测通过，未放宽产品断言。任务 4 合并回归为
  `73 passed, 30 warnings in 124.28s`，skipped=0；相关 Ruff 全绿，strict mypy
  `Success: no issues found in 10 source files`。

## 2026-07-29 任务 5：结构消融与定参边界

- [x] 红证据：新增消融测试首次收集因
  `ModuleNotFoundError: evaluation.chunking_ablation` 退出 1；实现后
  `9 passed in 1.33s`。直接脚本首轮又因未引导 `src/` 导入路径退出 1，修复
  direct-script 根路径后真实命令退出 0。
- [x] structural mode 固定比较 A legacy `384/512/64` 与 B/C/D 三个 v2
  `256/512/32`、`320/512/48`、`384/512/64`；支持重复 `--candidate`，只输出
  聚合 JSON。报告含角色、text/embedding 分位数、短块、section/group、hard max、
  覆盖、重复字符、表格行、空块、重复 ID、引用歧义和自动编号全部字段。
- [x] 真实 6 DOCX 结构报告保持 parser 计数 documents=6、headings=226、
  paragraphs=579、tables=71、images=132、unique_media=126、blank=0。
  Legacy=894 chunks、`<64` 比例 0.889262、standalone headings=226；
  B/C/D 分别为 241/238/236 chunks，`<64` 比例
  0.406639/0.411765/0.415254。
- [x] 三个 v2 候选的 standalone heading、cross-section、cross-group link、
  hard-max、uncovered element、blank、duplicate ID、普通表格行切断和 quote locator
  contract violation 均为 0，coverage=1.0，普通正文重复字符比例=0；相对 legacy
  短块比例严格下降且总块数未增加。
- [x] retrieval mode 仅调用 `load_tuning_cases()`，显式拒绝任何非 tuning case；
  文档键映射由不含题目/标签的独立 JSON 提供。每候选使用随机临时 collection 与
  独立临时 SQLite state，直接删除且从不创建/切换 alias；使用真实
  embedding/reranker，计算 Recall@5/10/20、MRR、rerank Recall@5，并单列
  cross_chunk/table/numeric。当前禁止联网且无已核验模型环境，未运行、未读取
  holdout、未选择或冻结参数。

## 2026-07-29 任务 6：文档与最终验收

- [x] 新增 `design/public/chunking-strategy.md`，记录 legacy、section/run/atomic
  边界、表格/OCR 策略、source span 与 locator 契约、活动证据可信根、消融方法、
  provisional 定参状态和已知限制；README 同步 structural/retrieval 操作命令。
- [x] 第二轮全量门禁在入口修复前为：compileall 退出 0，Ruff
  `All checks passed!`，Google docstring `missing_google_sections=0`，mypy
  `Success: no issues found in 62 source files`，pytest
  `291 passed, 35 warnings in 152.28s`、skipped=0；`bash -n deployment/*.sh`、
  带 `.env.example` 的 `docker compose config -q`、8/8 资产 SHA 和
  `git diff --check` 均退出 0。
- [x] 最终复跑发现 `chunking_experiment.py` 按文档直接执行时缺少 `src/` 导入路径，
  红证据为 `ModuleNotFoundError: No module named 'rag_app'`、退出 1；只增加与
  ablation 一致的 direct-script 路径引导及仓库外启动回归。专项结果
  `4 passed in 1.85s`，Ruff、Google docstring、strict mypy 均退出 0。
- [x] 修复后同一真实 6 DOCX 命令退出 0：section-pack-v2=236 chunks，
  legacy=894 chunks，fixed-512=70 windows；section text
  p50/p90/p95/max=110/339/391/455，embedding=127/346/400/469，状态仍为
  `structural_only_provisional`。
- [x] 最终 DOCX 审计退出 0：documents=6、bytes=22,358,173、headings=226、
  paragraphs=579、tables=71、image references=132、unique media=126、
  blank=0；automatic numbering 268 项仍按 P1 阻塞，不伪造 marker。
- [x] 第三轮（最终轮）完整验收退出 0：compileall、Ruff
  `All checks passed!`、Google docstring `missing_google_sections=0`、mypy
  `Success: no issues found in 62 source files`、pytest
  `292 passed, 35 warnings in 149.79s`、skipped=0；shell syntax、Compose
  config、8/8 资产 SHA、`git diff --check` 同轮均退出 0。随后按任务书精确范围
  补跑 `.venv/bin/python -m compileall -q src tests scripts evaluation` 和
  `.venv/bin/mypy --no-incremental src evaluation scripts`，后者为
  `Success: no issues found in 82 source files`。为遵守完整验收最多三轮，最终
  pytest 使用等价 console entrypoint `.venv/bin/pytest -q`，未为改写入口形式
  发起第四轮。
- [x] 独立临时 Git index 纳入全部候选后，发布安全扫描
  `passed=true`、tracked_files=188，private path/network/local path/secret/
  binary/large file 和 violations 均为 0；真实 `.git/index` 前后 SHA 均为
  `325776a830ac7d8558c5d62587ae82c761fdc30cd806884e33cfb60388fbe38c`，
  staged=0。
- [x] 保护范围终检未漂移：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts 保持任务 0 已阻塞的当前值
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`；
  既有验收文件
  `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`。
- [x] 参考仓库保持 HEAD
  `03d51db2c0e57ade04c8f9fe035316907d2717f5`、tree
  `84a0a960426da37111a93a806242543c61a881a9`、tracked 聚合
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  tracked diff 为空。
- [x] 本轮创建的本地真实 Qdrant v1.18.3 测试容器
  `rag-test-qdrant-section-v2` 已精确删除，remaining=0；没有删除镜像或共享数据。
- [x] 当前 HEAD 仍为
  `4fe7b26164e6ad1ee6b1f8477beed0473f7d49fe`，staged=0；未 commit、未 push、
  未 build/save/package、未访问服务器。一次禁网边界偏差、真实检索消融、自动编号、
  OCR/EMF/生产模型以及 `evaluation/metrics.py` 白名单硬冲突均在
  `BLOCKED.md` 如实保留。

## 2026-07-29 后续授权：本地功能提交

- [x] 用户在最终验收后明确要求 commit，覆盖此前“本轮不 commit”的结束状态；
  只创建本地提交，不 push、不联网复核远端。
- [x] 按仓库 Conventional Commits 与功能边界拆分，每个提交真实
  `git diff-tree --numstat` 变更量均小于 2000 行：
  - `a0653fd`：section-aware 分块契约，1808 行；
  - `b257860`：真实 legacy/fixed 分块基线，726 行；
  - `7d75de4`：候选分块消融，1730 行；
  - `54ba9c4`：现场活动证据可信根，1121 行；
  - `362e511`：source span 索引与引用校验，1011 行。
- [x] README、公开分块设计、完整 PROGRESS/BLOCKED 作为最后的文档提交；
  白名单冲突、外部模型/OCR/EMF、自动编号和既有禁网偏差仍保留，不因 commit
  被标记为解除。

## 2026-07-29 Query Trace v1 任务 0：事实基线

- [x] Git 基线符合任务书：HEAD
  `379210cbd16d36ebbca488014218847d5157e856`，tracked=188、
  untracked=0、staged=0、完整 `git status --short` 为空；真实 Git index
  SHA256 为
  `b91d7c840ac124199364fe14097b742f86c6c0906b50f6a831a58da61ad005db`。
- [x] 保护摘要未漂移：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`；
  既有验收文件
  `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`。
- [x] ignored/参考摘要未漂移：应用资产 8/8、OCR wheels 59/59、
  OCR models 6/6 和 OCR 总清单均逐项 `OK`；参考仓库 HEAD/tree/tracked 聚合为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  status 为空。OCR 清单首次在错误目录执行导致 59 个 `FAILED open or read`；
  更正到 `deployment/ocr/assets/wheelhouse` 后全部通过，未修改资产。
- [x] 静态/发布基线退出 0：compileall、Ruff `All checks passed!`、
  strict mypy `Success: no issues found in 82 source files`、Google docstring
  `missing_google_sections=0`、shell syntax、Compose config、应用资产 SHA 和
  `git diff --check`。临时 index release-safety 为 tracked_files=188、
  violations=0，真实 index 前后 SHA 相同。
- [x] 本地已有 `qdrant/qdrant:v1.18.3` amd64 镜像，以 `--pull never` 启动
  `rag-trace-baseline-qdrant`；全量精确命令退出 0：
  `292 passed, 35 warnings in 174.02s`、skipped=0，warning 类别未新增。
- [x] 现有追踪调用点：`query_service.py` 生成 6 类 `StageEvent`，
  `stream.py` 转发阶段/终态，`observability.py` 写 query_stage/query_outcome/
  external_call/query_failed JSON，`app.py` 生成 32 位 trace ID，
  `frontend/app.js` 仅展示阶段名与 `elapsed_ms`。
- [x] 全仓搜索确认没有 `TraceStore`、持久 span、`parent_span_id`、Trace 查询
  API、Debug 页面或 OTLP exporter；现有 `ExternalCallAudit` 只含净化前 endpoint、
  retry_count 和累计秒数。
- [x] 公开 ASCII 合成查询实际退出 0：事件顺序为 rewrite/retrieve/rerank/
  assemble/validate/complete，累计 `elapsed_ms` 为 0/0/0/0/0/5；审计共 7 条，
  终态 refused/NO_EVIDENCE。rerank `candidate_count=0` 实际来自 final hits；
  rewrite/route/neighbor/evidence 无稳定详细 reason，失败日志只保留异常类型。
  第一次内联脚本因缺 `PYTHONPATH` 退出 1，第二次因 PowerShell 中文转码触发替身
  断言退出 1；显式 `PYTHONPATH` 并使用纯 ASCII 公开数据后通过。

## 2026-07-29 Query Trace v1 顺序与风险（8 行）

1. 先以严格枚举和 SQLite 外键冻结 Trace/Span/Decision/Artifact 持久契约。
2. 再用单 writer 队列隔离普通查询降级与 FULL Debug fail-closed 语义。
3. 在不改变算法返回值的前提下，为每一现有阶段增加观察器和稳定 reason code。
4. 候选漏斗从各组件的现有确定性中间值复制，不重算、不改排序。
5. 管理 API 只接受 admin token，并对 FULL、trace 归属、分页和过期严格校验。
6. Debug 页面只用本地资源和 `textContent`，普通首页只显示可复制 trace ID。
7. 最后用四条公开合成 Trace 证明回答、拒答、预算丢弃和 repair 可回溯。
8. 最大风险是异步持久化不影响正常回答、FULL 容量原子准入和失败 span 尽力 flush。

## 2026-07-29 Query Trace v1 任务 1：持久契约

- [x] 红证据：新增 Trace model/store/recorder 契约测试后首次收集退出 1，
  `3 errors in 0.11s`，三项均为
  `ModuleNotFoundError: No module named 'rag_app.tracing'`；未放宽或删除测试。
- [x] 实现严格枚举、独立 SQLite 四表、zlib 压缩 artifact 与 SHA256 复核、
  FULL 72h/SAFE/DIAGNOSTIC 30d 到期删除、稳定倒序分页、单 Trace 5MiB 原始
  artifact 硬上限、0600 数据库权限、单 writer 有界队列、关闭排空和 exporter
  失败隔离；FULL 在 Store/队列不可用时于查询前 fail-closed，普通模式仅审计
  `TRACE_CAPTURE_FAILED`。
- [x] 首轮实现定向测试为 `2 failed, 7 passed`：一项错误假设短 zlib 结果必小于
  原文，另一项错误构造了与期望不符的时间顺序；改为验证精确压缩字节数并修正
  固定时间数据后，未改产品语义。最终定向门禁均退出 0：
  `9 passed in 0.54s`、Ruff `All checks passed!`、mypy
  `Success: no issues found in 9 source files`、compileall 无输出、
  Google docstring `missing_google_sections=0`。

## 2026-07-29 Query Trace v1 任务 2：Span 树与决策漏斗

- [x] 不改变查询算法输出，给现有返回对象附加 `compare=False` 旁路诊断：
  rewrite 的 8 个稳定 reason、路由逐规则命中数/覆盖率/阈值、dense/BM25
  独立 raw rank/score、RRF contribution/score/rank、rerank input/scored/final
  三个数量、neighbor 的 7 类接受/淘汰原因、evidence 的 5 类原因及
  OCR/source-span/token 字段、首次 validation 与唯一 repair 关联。
- [x] 每次启用 Trace 的查询建立 `rag.query` 根节点及 context/rewrite/route/
  retrieve/embedding/各 Qdrant 通道/RRF/rerank/neighbor/evidence/answer/
  validation/repair/publish 父子 span；span 保存独立 duration，原 StageEvent
  `elapsed_ms` 继续保持外部 NDJSON 兼容，但日志和前端明确标为请求累计时间。
  rerank 阶段事件已改为分别给出 input/scored/final，未再把 final hits 冒充
  candidate count。
- [x] SAFE/DIAGNOSTIC 不落业务原文；DIAGNOSTIC 仍保存完整候选 rank/score/
  reason；FULL 用独立压缩 artifact 保存准确 context/rewrite/retrieval/
  rerank/evidence/Prompt/原始模型输出/validation/final，显式排除向量、二进制、
  image/OCR base64 和凭据。查询异常关闭活动 span，根记录保存稳定
  `failure_stage`/error code，前序 span 与漏斗不丢失。
- [x] 首次组合命令因 120 秒上限退出 124、没有产出断言摘要；拆分单测确认两条
  Trace pipeline 测试各自约 1 秒通过，随后同一真实 Qdrant 容器下定向回归退出
  0：`56 passed, 2 warnings in 8.34s`。额外 mypy 定向检查为
  `Success: no issues found in 17 source files`；两类 warning 均为任务 0 已有
  TestClient/httpx2 与 HTTP API-key 类别。

## 2026-07-29 Query Trace v1 任务 3：管理员 API

- [x] 新增仅 admin token 可用的 FULL Debug Chat、Trace 分页/过滤、详情、
  trace 绑定 artifact 和 canonical export；query token 对五类管理员入口均
  401，所有管理响应均 `Cache-Control: no-store`。
- [x] FULL Debug 在查询提交前检查 recorder、Store 和队列；关闭真实 Store 后
  实测 HTTP 503 且 query `debug_calls=0`。跨 Trace artifact 返回 404，到期
  artifact 返回 410；详情只内联 artifact metadata，完整 payload 必须走绑定
  读取接口。
- [x] API/原 chat/Trace pipeline 定向回归退出 0：
  `13 passed, 1 warning in 2.69s`；管理员 list/detail/artifact/export 均用
  真实 SQLite，未 mock 持久化或鉴权。

## 2026-07-29 Query Trace v1 任务 4：本地 Debug 页面

- [x] 新增 `/debug/` 及本地 CSS/JS：列表筛选分页、诊断摘要、父子 waterfall、
  候选漏斗、artifact 输入输出、chunk/expected chunk 浏览器内诊断均不依赖
  日志；业务内容只通过 `textContent`/DOM 节点展示，无 `innerHTML`、CDN、
  远程字体或第三方库。
- [x] 普通首页新增可复制 trace ID，并把阶段毫秒明确显示为“请求累计”；
  query token 仍无 FULL Trace 读取入口。前端静态反测通过，`/debug/` 与本地
  `debug.js` 均 HTTP 200；本环境没有 `node`，额外 `node --check` 退出 127，
  该命令不属于任务书门禁，最终仍由前端源规则测试和 HTTP 验收覆盖。
- [x] 更新 `deployment/ASSETS.sha256` 纳入 6 个前端文件；新 SHA 均由实际
  `sha256sum` 输出生成，待最终 11/11 资产门禁复核。

## 2026-07-29 Query Trace v1 任务 5：可插拔导出边界

- [x] 新增无第三方依赖的 `TraceExporter` Protocol 和默认
  `NullTraceExporter`；导出发生在 Trace 终态持久化之后，异常只记录
  `TRACE_EXPORT_FAILED`，不会回滚 Store 或影响查询。
- [x] `design/public/trace-observability.md` 已固定内容边界、安全边界、
  SQLite 生命周期、失败语义和后续 OTel/Phoenix 映射；当前没有增加 SDK、
  服务、镜像或依赖。生产 evaluator 目录对 `rag_app.tracing`、`TraceStore`、
  `TraceExporter` 和 `trace_database` 的源扫描结果为 0。

## 2026-07-29 Query Trace v1 任务 6：反向测试与最终验收

- [x] 公开合成 SQLite 实际写入并读回 4 条 Trace：正常回答为 `ANSWERED` 且有
  4 阶段候选决策；无召回为 `REFUSED/RETRIEVAL_EMPTY`；预算丢弃为
  `TOKEN_BUDGET`；首次校验失败后存在 `REPAIR_OK` 子 span。两条 FULL Trace
  各有 1 个压缩 artifact，摘要不含问题或正文。
- [x] 反向测试覆盖 SAFE/DIAGNOSTIC/FULL 内容边界、query/admin 鉴权、跨 Trace
  404、过期 410、普通 Store 故障结果不变、FULL 预检 503 且查询未执行、队列满、
  artifact 超限不截断且不使回答失败、TTL/prune、失败 span、独立阶段耗时、
  rerank 三类数量、rewrite/route/neighbor/evidence reason、唯一 repair、
  secret/向量/OCR base64 净化、Trace 开关结果不变、前端无远程资源/innerHTML、
  writer 关闭和 export 不进入 evaluator。
- [x] 最终父子时间反测先在完整验收第 2 轮暴露亚毫秒壁钟/单调时钟偏移：
  `1 failed, 322 passed, 35 warnings in 154.99s`；改为同一壁钟区间向上取整后，
  定向回归为 `11 passed in 2.19s`。最终第 3 轮为
  `323 passed, 35 warnings in 154.08s`、skipped=0；高于任务 0 的 292 项基线，
  warning 数量和类别未新增，按上限不再发起第 4 轮。
- [x] 最终静态门禁均退出 0：compileall 无输出、Ruff
  `All checks passed!`、mypy
  `Success: no issues found in 88 source files`、Google docstring
  `missing_google_sections=0`、全部 deployment shell、Compose config、
  `git diff --check`；应用资产更新后为 11/11 `OK`。
- [x] 临时 Git index 纳入全部候选后 release-safety 为
  tracked_files=204、binary/large/local-path/private-network/private-path/
  secret/总 violations 全为 0；真实 index 前后 SHA256 均为
  `b91d7c840ac124199364fe14097b742f86c6c0906b50f6a831a58da61ad005db`。
  当前 49 个变更条目全部命中任务白名单。
- [x] 保护摘要终检未漂移：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts 保持已阻塞值
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`；
  既有验收文件
  `9e596c21953d3181992db3b4c96beb55d7d8c1ce368a0eea9b742a915105f6ab`。
- [x] ignored OCR wheels 59/59、models 6/6、总清单 69/69 和冻结集 1/1 均为
  `OK`。wheels 首次使用错误相对层级而退出 1，改为
  `../../WHEELS.sha256` 后全绿，未修改资产。参考仓库保持 HEAD/tree/tracked
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  status 为空。
- [x] 本轮真实 Qdrant 测试容器 `rag-trace-baseline-qdrant` 无挂载，已精确删除，
  remaining=0；没有删除镜像、卷或共享数据。本轮未联网、未 build/save/package，
  未访问 `.57/.58/.60`，`BLOCKED.md` 继续保留外部验收项。

## 2026-07-29 后续授权：Query Trace v1 本地提交

- [x] 用户在验收后明确要求“按要求 commit”，覆盖任务书原先“不 commit”的结束
  状态；授权仅扩展到本地提交，没有据此 push 或联网复核远端。
- [x] 按功能边界和仓库 Conventional Commits 拆分，逐个执行 staged
  `git diff --check`，且每个提交新增+删除均小于 2000 行：
  - `5dfc41b`：Trace model/reason/SQLite Store 契约，1818 行；
  - `800be82`：单 writer recorder、exporter 与四类合成 Trace，1432 行；
  - `045d133`：rewrite/retrieval/rerank/neighbor/evidence 决策漏斗，1357 行；
  - `958b578`：查询 span、失败路径与 Trace 开关不变性，1636 行；
  - `cff53e7`：管理员 API、运行时接线和本地 Debug 页面，1354 行。
- [x] README、公开可观测性设计、部署说明、完整 PROGRESS/BLOCKED 作为单独文档
  提交；所有外部模型、OCR/GPU/EMF、自动编号、chat-template、旧 artifacts 和
  服务器负载项继续保留，没有因提交而标记为解除。

## 2026-07-29 离线发布链 P0：目标、顺序与最大风险（8 行）

1. 只修 Qdrant healthcheck、worker 启动策略、rollback、sidecar、SBOM 预检和备份。
2. 顺序固定为事实基线，再按 P0-1 至 P0-6 逐项红测、最小修复和专项绿测。
3. Qdrant 探针只使用固定 v1.18.3 镜像内实测存在的命令，并做正反真实容器验证。
4. worker 保留 provisional 严格拒绝门禁，只从默认启动路径移到显式 index profile。
5. rollback 先验证旧 release/镜像，再原子持久化旧镜像并补偿元数据提交失败。
6. package 的 SBOM 能力预检先于一切正式输出，解包器使用独立 SHA sidecar。
7. backup 只提升源数据读取权限，验证归档和 SHA 后才原子发布并恢复原服务集合。
8. 最大风险是 Shell 事务补偿、fake 命令与真实 Compose/Docker 语义出现偏差。

## 2026-07-29 离线发布链任务 0：事实基线

- [x] HEAD 为预期 `da9240ab48a9f10607210425ee092c7eeb9e0ff2`；初始
  tracked=204、untracked=0、modified=1、deleted=0、staged=0，完整 status
  仅为 `BLOCKED.md` 已修改。真实 `.git/index` SHA256 为
  `47069465c413de9f984caa50008286f70f7a9e505f6817e57a66ce828c2da806`。
- [x] 保护摘要未漂移：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
- [x] 参考仓库 HEAD/tree/tracked 聚合分别为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`，
  status 为空。
- [x] compileall、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 88 source files`、Google docstring
  `missing_google_sections=0`、全部 deployment Shell、默认 Compose config、
  11/11 应用资产和 `git diff --check` 均退出 0。
- [ ] 全量 pytest 三轮分别为 `13 failed, 310 passed`、
  `2 failed, 321 passed`、`1 failed, 322 passed`；第三轮只剩白名单外既有
  Query Trace 父子结束时间偶发断言，原始证据已置顶写入 `BLOCKED.md`。
- [x] 临时 Git index 发布扫描为 tracked_files=204、violations=0；真实 index
  前后 SHA 相同。固定 Qdrant digest 本地存在且为 `amd64/linux`，未 pull。
- [x] 红基线调用点确认：Qdrant 为 `CMD-SHELL` + `/dev/tcp`；默认 Compose
  包含 worker 且 worker 为 `restart: unless-stopped`；rollback 仅一次性注入
  `RAG_*_IMAGE`，未重验旧 release 或持久化 env；解包器无外置 sidecar；
  `docker sbom` 位于 tag/save 后；公开文档仅有手工 `tar -czf` 备份。
- [x] 当前 pipeline/retrieval/corpus SHA 分别为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c`、
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3`、
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`；
  本轮后续以完整 SHA 锁定，不修改 provisional/frozen 状态。

## 2026-07-29 离线发布链任务 1：P0-1 Qdrant healthcheck

- [x] 新增 `tests/test_qdrant_healthcheck.py` 后红测退出 1：
  `2 failed`；直接证明旧配置仍是 `CMD-SHELL`、`/dev/tcp`，且无法作为确定
  命令执行。
- [x] 固定 digest 镜像只读实测包含 `/usr/bin/grep`、`/proc/net/tcp*`；
  Compose 改用 `CMD` 直接检查 6333 十六进制端口 `18BD` 的 TCP
  `LISTEN (0A)`，不依赖 shell 扩展，也不受 API key 鉴权影响。
- [x] 专项绿测为 `2 passed in 0.12s`；默认 Compose config 和
  `git diff --check` 均退出 0。
- [x] 使用固定 digest、`--pull never`、API key、无挂载临时容器真实反测：
  正确端口容器为 `running healthy` 且连续探针 exit=0；错误端口 `FFFE`
  为 `running unhealthy` 且连续探针 exit=1。两个本轮 health 容器均已精确
  删除，remaining=0；未删除镜像、卷、网络或其他容器。

## 2026-07-29 离线发布链任务 2：P0-2 worker 启动策略

- [x] 新增 `tests/test_worker_deployment_policy.py` 后红测退出 1：
  `2 failed, 1 passed`；证明默认 Compose 仍包含 worker，且 worker 没有
  `index` profile。
- [x] `rag-worker` 现在只属于显式 `index` profile，继续与 app 使用同一
  `RAG_APP_IMAGE`，并改为 `restart: "no"`；app/OCR/Qdrant 的 restart
  策略和 app 依赖关系未变。
- [x] 默认 Compose services 实测仅为 `rag-qdrant/rag-app/rag-ocr`；显式
  `--profile index` 才增加 `rag-worker`。默认和 profile Compose config
  均退出 0。
- [x] 专项与部署契约绿测为 `7 passed in 0.12s`；pipeline/retrieval SHA
  仍为 `f61a74b0…` / `267e419f…`，`git diff --check` 退出 0。
- [x] 两份部署说明已明确 provisional 阶段默认只启动三项核心服务、
  `/ready=503` 为正确结果；只有冻结参数并核验模型 revision 后，才使用
  `docker compose --profile index ... rag-worker` 显式启动单索引 worker。

## 2026-07-29 离线发布链任务 3：P0-3 rollback 持久化

- [x] 新增 rollback 契约测试后红测退出 1：`3 failed`；证明旧脚本未重验
  verify/Compose/OCI 身份、没有持久 env、没有 worker 状态判断和补偿函数。
- [x] rollback 现在先校验 env/回滚记录为非 symlink 普通文件、旧 release
  固定路径、旧 `verify-offline.sh`、Compose、三个 `sha256:` 镜像 ID、
  app/OCR OCI revision 及 Qdrant source digest；任一失败不执行 compose up
  或修改 env/current/rollback 记录。
- [x] 仅替换三个 `RAG_*_IMAGE` 和已有的 `RAG_RELEASE_REVISION`；其他 token、
  路径和配置逐行保留。新旧 env 临时文件均在 shared env 同目录且为 0600，
  正式 env 不使用 `sed -i`。
- [x] 回滚前 worker 在运行时首次 up 显式使用 `--profile index` 并验证旧 app
  image ID；未运行时普通 up 不新增 worker。容器 ID、Compose ps 和 `/live`
  全绿后才提交 env/current。
- [x] env 替换、current rename 或提交后持久状态复核失败时，`restore_metadata`
  原子恢复原 env/current；补偿失败会明确非零。成功后又以正式 env 和
  `current/compose.yaml` 运行普通 config/up 并复核三镜像，防止重启切回新版。
- [x] 临时目录 fake docker/curl/mv 测试覆盖旧 verify 失败、镜像缺失、
  OCI revision 错误、env key 缺失/重复、compose up 失败、容器镜像错误、
  env replace/current rename/提交后 env 校验失败、普通 restart 持久选择及
  worker 旧 app 镜像；专项为 `15 passed in 0.92s`。
- [x] 合并部署契约回归为 `22 passed in 1.10s`；Ruff、rollback
  `bash -n`、`git diff --check` 均退出 0。未访问服务器或真实执行回滚。
- [x] 最终调用审计进一步覆盖旧 release 仍为修复前 Compose 的情况：两次
  rollback up 均显式列出 qdrant/OCR/app，只有原 worker 在运行时才追加
  worker，避免旧 Compose 默认启动它；新增反测后回归为
  `22 passed in 1.46s`。

## 2026-07-29 离线发布链任务 4：P0-4 解包器 sidecar

- [x] sidecar 红测退出 1：`2 failed, 4 passed`；证明 package 没有声明外置
  `offline_bundle.py.sha256`，公开上传/服务器校验流程也缺失该文件。
- [x] package 正式输出现在同时声明并拒绝覆盖 `offline_bundle.py` 及其
  `.sha256`，复制后用既有 `write_sidecar` 生成标准 basename
  `sha256sum` 记录；最终摘要增加 `unpacker` 和 `unpacker_sha`。
- [x] 正确脚本/sidecar 退出 0；脚本内容、digest、sidecar 文件名三类篡改均
  非零。公开手册的交付树、上传清单、本地和服务器流程均增加 sidecar，
  `sha256sum -c offline_bundle.py.sha256` 明确位于 Python 执行之前。

## 2026-07-29 离线发布链任务 5：P0-5 SBOM 预检前置

- [x] fake docker 红测退出 1：`3 failed, 1 passed`；当前源码没有
  `docker sbom --help`，SBOM 不可用时仍记录到 Qdrant image tag 调用。
- [x] 三张镜像的纯读取平台/ID/revision 校验完成后，立即执行
  `docker sbom --help >/dev/null`；它位于 image tag/save、artifact mkdir、
  runtime/corpus/SBOM 正式输出、tar 和全部 sidecar 之前。
- [x] fake 反测证明 SBOM 不可用时 tag_count=0、save_count=0、
  artifacts/不存在、正式归档和 sidecar 均为 0；SBOM 可用时调用顺序为
  inspect→`sbom --help`→tag，坏 image inspect 则在预检和写入前失败。
- [x] package/sidecar/deployment 合并回归为 `15 passed in 0.15s`；Ruff、
  package `bash -n`、`git diff --check` 均退出 0。未真实执行 package、tag、
  image save 或 SBOM 生成。

## 2026-07-29 离线发布链任务 6：P0-6 可靠备份

- [x] 新增 `tests/test_backup_script.py` 后红测退出 1：`2 failed`，均因
  `deployment/backup.sh` 不存在，锁定固定源目录、sudo 流、0600、SHA、
  原子发布、原运行集合恢复和 worker profile 契约。
- [x] backup 仅接受可选安全 backup ID 与 shared env 路径；state/Qdrant
  固定为项目根下目录。项目、数据、备份、release 目录均做 realpath、范围、
  symlink 及祖先 symlink 校验，既有 ID 和路径越界直接拒绝。
- [x] 记录 app/worker/Qdrant 的实际 `.State.Running` 后，使用显式
  `--profile index` 停止并确认三项写入服务；OCR 不停止。trap 在成功或失败
  时只以 `--no-deps` 恢复原来运行的服务，原未运行 worker 不会被新增。
- [x] state/Qdrant 通过 `sudo tar --format=posix ... -cf - | gzip > *.tmp`
  流式读取；归档非空、`gzip -t`、`tar -tzf/-tvzf`、固定顶层、无绝对/`..`
  路径且仅普通文件/目录后才定名。
- [x] 两个 0600 归档和 0600 manifest 强制归 `SUDO_UID/SUDO_GID` 表示的原
  调用用户，`sha256sum -c MANIFEST.sha256` 两次通过后才 `mv -T` 原子发布
  0700 目录。失败临时目录明确标记 incomplete，历史备份从不删除。
- [x] fake docker/sudo/tar/gzip/curl/sha 测试覆盖 umask/0600、backup 根
  symlink、既有 ID、state/qdrant 缺失、sudo 流、所有权、空归档、gzip/tar/
  SHA 失败、归档 symlink、失败恢复、worker 不误启、成功恢复、历史保留、
  最终目录延迟出现及恢复失败稳定 exit=70 且保留已验证备份。
- [x] backup 行为专项现为 `14 passed`；与 deployment/package 回归合计
  `22 passed in 1.61s`，Ruff、全部 deployment `bash -n`、
  `git diff --check` 均退出 0。
- [x] `backup.sh` 已进入 runtime 打包和 verify 必需文件；公开手册删除旧手工
  tar 命令并明确禁止，改为真实部署后执行脚本。本轮未在服务器执行备份、
  恢复或回滚。

## 2026-07-29 离线发布链：最终调用审计与门禁

- [x] 最终范围为 9 个白名单 tracked 修改和 7 个白名单新文件；`src/**`、
  `evaluation/**`、docs/artifacts/evidence、三份 deployment config、参考仓库
  均无改动，deleted=0、staged=0。
- [x] 调用审计确认生产 Compose 无 `/dev/tcp`/Qdrant `CMD-SHELL`；默认服务
  仅 qdrant/app/OCR，显式 index profile 才增加 worker；worker 为
  `restart: "no"`，其他三项 restart 未变。
- [x] rollback 两次 up 都使用显式服务集合，旧 Compose 也不会误启 worker；
  package 的 `docker sbom --help` 在 tag/save/正式输出之前，解包器 sidecar
  与上传校验顺序正确；备份文档只保留禁止手工 tar 的说明和 `backup.sh`。
- [x] 最终 compileall、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 88 source files`、Google docstring
  `missing_google_sections=0`、全部 deployment Shell、默认/profile Compose、
  11/11 应用资产、`git diff --check` 均退出 0。
- [x] 六项 P0 与部署契约专项为 `48 passed in 2.57s`，skipped=0；warning=0。
  固定 Qdrant 正确/错误探针真实为 healthy/unhealthy，相关临时容器及最终
  `rag-p0-baseline-qdrant` 均已精确删除，remaining=0。
- [ ] 全量 pytest 最终为 `1 failed, 366 passed, 36 warnings in 163.59s`，
  skipped=0；warning 类别与任务 0 相同。唯一失败是禁止修改的既有
  Query Trace 父子结束时间，定向复跑仍 `1 failed in 1.16s`，已置顶阻塞。
- [x] 最终保护摘要与任务 0 完全一致：docs `36c67e3…`、artifacts
  `220473c6…`、frozen `63adcd45…`、results `cdb17f0c…`、evidence
  `05b845b9…`；参考 HEAD/tree/tracked 聚合仍为 `03d51db2…` /
  `84a0a960…` / `44254dff…`。
- [x] HEAD 仍为 `da9240ab48a9f10607210425ee092c7eeb9e0ff2`，真实 index
  SHA256 仍为
  `47069465c413de9f984caa50008286f70f7a9e505f6817e57a66ce828c2da806`；
  工作树 tracked=204、modified=9、untracked=7、deleted=0、staged=0。
- [x] 最终临时 Git index 纳入全部候选后为 tracked_files=211，binary/large/
  local-path/private-network/private-path/secret/总 violations 全为 0；真实
  index 前后 SHA256 相同，临时 index 已删除。
- [x] 明确未执行 build/buildx、image save/load、真实 package、正式双包、
  SSH/SCP、`.57/.58/.60` 访问、服务器 backup/restore/rollback、
  commit/push；未 pull、安装依赖或生成服务器构建层。

## 2026-07-29 离线发布链续跑：备份发布竞态审计

- [x] 最终审计复现了检查目标不存在后、`mv -T` 前出现同名空目录的竞态：
  新增定向反测退出 1，原实现错误返回 0 并覆盖竞态目标。
- [x] `backup.sh` 改用 GNU `mv -Tn`，并在命令返回 0 后确认临时目录确已
  消失；目标竞态存在时非零退出、保留明确命名的 incomplete 目录且不发布
  manifest，不覆盖竞态目标。
- [x] `tests/test_backup_script.py` 全量回归为 `15 passed in 1.76s`，
  skipped=0、warning=0；本项未访问服务器或执行真实备份。
- [x] 纳入六项 P0 与部署契约后专项为 `49 passed in 3.12s`；专项 Ruff、
  全部 deployment Shell 语法和 `git diff --check` 均退出 0。

## 2026-07-29 离线发布链续跑：最终范围与非全量门禁

- [x] 默认/profile Compose config 均退出 0；服务清单分别为
  `rag-qdrant/rag-app/rag-ocr` 和追加 `rag-worker`。11/11 应用资产摘要
  全部 `OK`。
- [x] compileall 无输出、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 88 source files`、Google docstring
  `missing_google_sections=0`；Shell、Compose、资产与 `git diff --check`
  均为绿。
- [x] 保护摘要仍为 docs `36c67e3…`、artifacts `220473c6…`、frozen
  `63adcd45…`、results `cdb17f0c…`、evidence `05b845b9…`；参考
  HEAD/tree/tracked 仍为 `03d51db2…` / `84a0a960…` / `44254dff…`，
  参考工作树为空。
- [x] 临时 Git index 扫描为 `tracked_files=211`、`violations=0`，真实
  index 前后均为 `47069465…`，临时文件已清理。当前 status 为 tracked=204、
  modified=9、untracked=7、deleted=0、staged=0；16/16 条目均命中白名单。
- [x] 本轮没有新增或保留临时 Qdrant 容器；`docker ps -a --filter
  name=rag-p0` 输出为空。
- [ ] 未重跑全量 pytest：此前已经达到任务书规定的三轮完整验收上限，唯一
  Query Trace 失败仍在白名单外；原始失败与本次 49 项专项绿证据继续置顶保留
  于 `BLOCKED.md`。
- [x] 本轮续跑仍未执行 build/buildx、image save/load、真实 package、正式
  双包、SSH/SCP、`.57/.58/.60`、服务器 backup/restore/rollback、
  commit/push，也未 pull 或安装依赖。

## 2026-07-29 离线发布链续跑：阻塞终审

- [x] 第三个连续目标回合重新读取任务书、`PROGRESS.md` 和 `BLOCKED.md`；
  当前 16 个白名单变更、staged=0 和真实 index SHA `47069465…` 均未漂移。
- [ ] 唯一未满足项仍为全量 pytest 退出 0；修复需要新增
  `src/rag_app/tracing/**` 或既有 Query Trace 测试的白名单授权，而任务书同时
  禁止扩大范围并限制完整验收最多三轮。连续三回合均为同一不可绕过阻塞，
  当前没有剩余的范围内工作可以使该完成条件成立。

## 2026-07-29 合并后剩余阻塞任务 0：事实基线

- [x] 新任务授权后的 HEAD 为预期
  `4a8d4292d4aa2ef052a617c457f91c959c583f0e`；工作树为空，
  tracked=211、modified/untracked/deleted/staged 均为 0；真实 index SHA 为
  `9e780b9c89ac6e36c72c595ed2fa67dc575a46b81bfa46835c7735256a407ce1`。
- [x] 保护摘要仍为 docs `36c67e3…`、artifacts `220473c6…`、frozen
  `63adcd45…`、results `cdb17f0c…`、evidence `05b845b9…`；参考
  HEAD/tree/tracked 仍为 `03d51db2…` / `84a0a960…` / `44254dff…`，
  参考工作树为空。
- [x] compileall 无输出、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 88 source files`、Google docstring
  `missing_google_sections=0`；全部 Shell、默认/profile Compose、
  11/11 资产和 `git diff --check` 均退出 0。
- [x] 临时 index release-safety 为 `tracked_files=211`、
  `violations=0`；真实 index 前后 SHA 相同，临时 index 已删除。
- [x] 第 1 次可判定全量红基线为
  `1 failed, 367 passed, 36 warnings in 158.56s`；唯一失败是
  `test_safe_trace_has_complete_tree_without_business_artifacts`。失败发生在
  `recorder.close()` 前，非 daemon writer 使 pytest 摘要后等待；精确 SIGINT
  清理了本轮 PID，未删除或修改测试。
- [x] 调用点与预期一致：package 直接比较 Qdrant `.Id`/registry digest；
  rollback 对来源 digest 作同一假设；deploy 不处理 worker；rollback 仅有
  `restore_metadata`；backup 未读 `.State.Health.Status`；Compose 和 env
  仍允许 release/image `0.1.0` 默认值。

## 2026-07-29 合并后剩余阻塞：执行顺序与最大风险

1. 先建立单一 Trace timeline，稳定 200 次原失败用例并消除 writer 残留。
2. 再拆分 RepoDigest/image ID，同时收紧 40 位 release revision。
3. 之后实现 deploy 的 worker/核心/元数据完整失败补偿。
4. 在 deploy 记录的 worker 状态基础上实现 rollback 双目标联合补偿。
5. 最后让 backup 严格按 Qdrant healthy→app live→worker running 恢复。
6. 最大风险是 Shell 补偿的二次失败留下容器与 env/current 不一致。
7. 次要风险是 Trace 外部 duration 超过父区间时破坏层级或结果不变性。
8. 完整验收最多再使用两轮；不以重复运行碰时序运气。

## 2026-07-29 合并后任务 1：Trace 单一时间轴

- [x] 新增 `tests/test_trace_time_invariants.py` 后红测为 `8 failed`：
  确定复现 wall clock 二次读取、父子结束倒挂、child 早于 parent、外部
  duration 越界、关闭 parent 后仍可建 child，以及 Trace finish 早于根 span。
- [x] `TraceSession` 现在以 `trace.created_at` 和一次 monotonic 读数冻结双锚点，
  所有 span/根 Trace 时间均由同一 helper 毫秒向上量化；不再在
  start/finish/completed 中调用 `datetime.now()`。
- [x] 会话内记录父子和开闭状态；父关闭前要求后代已关闭并覆盖最大 finish，
  关闭 parent 下创建 child 直接 `RuntimeError`。外部 duration 超出可用区间时
  实际 span 被夹取，原值写入 `reported_duration_ms`。
- [x] fake clock 覆盖 0ms、亚毫秒、整毫秒、wall clock 跳变、边界 child、
  多层、失败和关闭父节点；200 次真实时钟合成查询证明树合法且 Trace 开关
  不改变 QueryOutcome 或模型调用。
- [x] 原始
  `test_safe_trace_has_complete_tree_without_business_artifacts` 未修改断言，
  连续调用 200 次为 `1 passed in 43.16s`。
- [x] 合并 Trace 回归为 `13 passed in 67.90s`；专项 Ruff、mypy、
  Google docstring 和 `git diff --check` 全绿。旧 Query Trace 阻塞已从
  `BLOCKED.md` 删除。

## 2026-07-29 合并后任务 2/6：Qdrant 双身份与 revision 契约

- [x] package fake 改为 registry digest `0bd98f…` 与本地 image ID
  `777777…` 刻意不同；连同新身份、Compose/env 和 RuntimeSettings 反测，
  红基线为 `10 failed, 24 passed`。
- [x] package 继续要求批准的完整输入引用，并新增 canonical
  `RepoDigests` 精确包含检查；`.Id` 只校验合法格式并继续写入 Qdrant TSV。
  错误 RepoDigest 在 SBOM/tag/save/正式输出前失败。
- [x] rollback 保留并校验 `QDRANT_SOURCE_IMAGE` 来源记录，但实际本地身份改为
  读取旧 release `IMAGE_ARCHIVES.tsv` 第三列；测试中的 source digest 与
  image ID 已明确不同，不再直接比较两者。
- [x] Compose 四个 image 表达式和两处 `RAG_RELEASE_REVISION` 均改为必填；
  `.env.example` 删除 `0.1.0`，revision 改为 40 位 SHA 占位符。
- [x] `RuntimeSettings.release_revision` 取消默认并严格匹配
  `^[0-9a-f]{40}$`；缺失、短 SHA、大写和 `0.1.0` 均红，完整小写 40 位绿。
  serving/pipeline fingerprint 代码与三份配置未改。
- [x] 相关回归为 `56 passed, 1 warning in 4.41s`，package 合并回归
  `16 passed`；专项 Ruff/mypy、三个 Shell、默认/profile Compose 和
  `git diff --check` 全绿。
- [x] deploy 的 revision/SOURCE_REVISION 错配在 load/up 前失败；fake 状态机
  覆盖缺失、短 SHA、大写、`0.1.0`、合法但错配五种反测。

## 2026-07-29 合并后任务 3：deploy worker 与失败事务

- [x] fake 状态机红基线为 `9 failed, 2 passed`；实现后覆盖 worker 不存在、
  停止、运行以及核心全有/全无/不完整集合，最终为 `17 passed in 2.17s`。
- [x] 任何容器修改前保存旧 release、三核心实际 image ID 和
  `ROLLBACK_WORKER_WAS_RUNNING`；运行 worker 必先停止并确认，新部署成功后
  worker 保持停止，显式 index profile 契约未改。
- [x] load、加载后 Qdrant ID 漂移、核心部分 up、ps、current rename 失败时，
  使用旧 current Compose 和实际 image ID 恢复核心、worker、env、current；
  类别稳定为 `DEPLOY_FAILED_RECOVERED`/`DEPLOY_FAILED_RECOVERY_FAILED`。
- [x] 核心全无但孤立 worker 运行也已覆盖：成功时先停 worker，失败时删除
  新建核心并以 `--no-deps` 恢复原 worker；没有 worker 时不会创建 worker。

## 2026-07-29 合并后任务 4：rollback 运行时与元数据联合补偿

- [x] rollback 严格读取且只读取 deploy 持久化的
  `ROLLBACK_WORKER_WAS_RUNNING=true|false`，不再用调用时 worker 状态推断
  回滚目标；缺失、重复、非布尔均在容器修改前失败。
- [x] 旧 Compose up 前冻结调用时的新 release、原 env、三核心实际 image ID、
  worker 存在/运行/image ID，分别构造 rollback target 和 compensation target。
- [x] 旧 Compose 部分切换、镜像核验、`/live`、env/current 原子切换和持久复核
  失败均恢复三核心、worker、env、current；补偿失败使用独立稳定 exit=70。
- [x] 测试实际读取 fake container state，不只检查文件；运行 worker、无 worker、
  核心镜像和元数据恢复专项连同既有回滚回归为 `26 passed in 3.71s`。

## 2026-07-29 合并后任务 5：backup 健康恢复顺序

- [x] Qdrant 原运行时先单独启动，最多轮询 `.State.Health.Status` 30 次；
  仅 `healthy` 继续，`unhealthy` 立即失败，持续 `starting` 固定超时失败。
- [x] Qdrant healthy 后才启动 app 并检查 Running 与 `/live=200`；随后仅在
  原 worker 运行时通过 index profile 恢复。原未运行服务不新增，不查 `/ready`。
- [x] fake 反测覆盖 starting→healthy、直接 unhealthy、30 次超时、app live
  失败和 worker 两种状态；backup 合并回归为 `21 passed in 3.05s`。
- [x] 当前部署/打包/回滚/备份契约专项为 `94 passed in 9.36s`，skipped=0；
  未执行真实 load、package、backup、rollback 或任何服务器操作。

## 2026-07-29 合并后最终验收与交付审计

- [x] 第一轮完整绿验收为 `419 passed, 36 warnings in 233.95s`；联合补偿随后
  收紧为容器补偿失败时仍独立尝试元数据补偿，回滚专项保持
  `26 passed in 3.47s`，因此按规则再执行一次完整验收。
- [x] 最终完整验收为 `419 passed, 36 warnings in 232.45s`，skipped=0；
  warning 数量和类别与任务 0 红基线相同，没有使用第三轮验收额度。
- [x] 最终静态门禁全部退出 0：compileall 无输出、Ruff
  `All checks passed!`、strict mypy `68 source files`、Google docstring
  `missing_google_sections=0`、全部 deployment Shell、默认/profile Compose、
  11/11 ASSETS 和 `git diff --check`。
- [x] 首个临时 index 命令因 PowerShell→WSL 转义失败，输出
  `tracked_files=0` 和 fatal，明确不作为证据；改用临时脚本后首次真实扫描发现
  fake fixture 的凭据变量字面赋值触发 `violations=1`。改为非凭据
  `CUSTOM_SETTING=preserve` 后对应部署测试仍为 `17 passed`。
- [x] 最终临时 index 扫描为 `tracked_files=216`，binary/large/local-path/
  private-network/private-path/secret 均为 0、`violations=0`；真实 `.git/index`
  前后 SHA256 均为
  `9e780b9c89ac6e36c72c595ed2fa67dc575a46b81bfa46835c7735256a407ce1`。
- [x] 冻结摘要未漂移：docs `36c67e3b…`、artifacts `220473c6…`、
  frozen `63adcd45…`、results `cdb17f0c…`、evidence `05b845b9…`；
  pipeline/retrieval/corpus 分别仍为 `f61a74b0…` / `267e419f…` /
  `0d6553c1…`，retrieval 继续为 provisional。
- [x] 参考仓库 HEAD/tree/tracked 仍为 `03d51db2…` / `84a0a960…` /
  `44254dff…` 且状态为空；本仓库 HEAD 仍为 `4a8d4292…`，staged/deleted=0。
- [x] 两个本轮临时 Qdrant 容器均为 mounts=0，已按精确名称删除并确认
  remaining=0；临时验收日志、临时 index 和 helper 已清理，未删除镜像、卷、
  网络或共享数据。
- [x] 本轮未执行 build/buildx、真实 save/load/package、SSH/SCP、`.57/.58/.60`、
  服务器 backup/rollback、安装、下载、commit、push 或 pull。

## 2026-07-29 后续授权：提交并推送合并阻塞修复

- [x] 用户在完整验收后明确要求 commit 并 push，覆盖上一节验收时点的
  “未 commit/push”限制；授权不扩展到构建、服务器或其他外部操作。
- [x] 按仓库 Conventional Commits 中文标题和功能边界拆分五个代码提交：
  `8359ad9` Trace 时间边界、`d202e08` 镜像身份/revision、`e532c8e`
  deploy 事务、`af217a4` rollback 联合补偿、`3c95410` backup 健康恢复。
- [x] 每个提交前均执行 `git diff --cached --check`；变更行数分别为
  552、129、1024、776、288，全部小于 2000 行，未混入范围外文件。
- [x] 首次推送退出 0：`4a8d429..42941ed  main -> main`；随后提交本条
  推送证据并再次推送，最终以远端 `refs/heads/main` 与本地 HEAD 完整 SHA
  一致作为完成证据。

## 2026-07-30 最终生产包一致性任务 0：事实基线

- [x] 起始 HEAD 为预期
  `49c34074a0553711bae4796aeb42da3916f31623`；工作树为空，
  tracked=216、untracked/modified/deleted/staged 均为 0；真实 Git index
  SHA256 为
  `dee80a74563a99d765fb3d34ce87860a6bf068a73ed20d7bfadcbd76d3be8b8f`。
- [x] 保护摘要未漂移：docs `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`、
  artifacts `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`、
  frozen `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`、
  results `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`、
  evidence `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
- [x] 三份冻结配置 SHA256 分别仍为 pipeline `f61a74b0…`、retrieval
  `267e419f…`、corpus policy `0d6553c1…`；retrieval 保持 provisional。
  参考仓库 HEAD/tree/tracked 聚合仍为 `03d51db2…` / `84a0a960…` /
  `44254dff…`，tracked=182、状态为空。
- [x] 固定 Qdrant 镜像实测为
  `sha256:0bd98fa…`、`amd64/linux`；临时容器使用 `--pull never`、
  mounts=0、仅绑定 `127.0.0.1:6333` 并启用测试 API key。
  未启动依赖时首轮为 `32 failed, 387 passed, 36 warnings`，全部 32 项均为
  6333 返回 502；依赖就绪后同一完整命令为
  `419 passed, 36 warnings in 253.86s`，skipped=0。
- [x] compileall 无输出、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 88 source files`、Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/profile
  Compose、11/11 应用资产及 `git diff --check` 均退出 0。
- [x] 当前 HEAD 的 release-safety 首次因本文件历史证据含凭据变量字面赋值而
  `violations=1`；改为等义非赋值描述后为 `tracked_files=216`、
  `violations=0`。临时 Git index 纳入本轮进度文件后同样为 216/0，
  真实 index 前后 SHA256 均为 `dee80a…`，临时 index 已删除。
- [x] 索引调用审计确认：INCREMENTAL 直接选择活动 collection/state；
  成功 item 会立即调用 `activate_source_version`、rename/delete 会立即修改
  活动 payload，失败 item 只阻止 `record_active_revision`，不会回滚已成功项；
  FULL 使用空 previous sources；全量才调用 `FullIndexPublisher`。
- [x] 租约调用审计确认：control job 在完整 `_run_claimed()` 期间没有
  heartbeat；local job 只在每个 item 前调用 `renew_job_lease`；
  `claim_next_job`/`finish_job` 均只校验当前 owner，长 item 可越过租约。
- [x] 部署调用审计确认：rollback state 在 `perform_deploy` 前正式替换；
  recovery env 从传入活动/候选混用的 env 复制；current 切换前只校验 running，
  未等待 Qdrant/OCR/app health；rollback 要求当前三核心全 running。
- [x] 构建/包审计确认：wheel 不含完整 Git revision，Dockerfile 只校验
  `VCS_REF` 格式；package 含数量 6、总字节 22358173，并直接向最终
  artifacts 文件写 tar/sidecar，没有整体原子发布或外置 corpus manifest。

## 2026-07-30 最终生产包一致性：目标、顺序与最大风险（8 行）

1. 先让 full planner 继承可靠 source identity，同时强制所有文档重新构建。
2. 再把增量改为 snapshot/SQLite backup 驱动的新 collection copy-on-write。
3. 在统一发布边界上加入 control/local 两层非 daemon 租约 heartbeat。
4. 随后重做 candidate env、健康提交、rollback state 与 degraded 补偿事务。
5. 再把 Git HEAD 固化进 wheel，并与 OCI VCS_REF 和三镜像身份交叉校验。
6. 最后外置 corpus manifest、原子发布完整输出、不可变安装并补 backup 身份。
7. 最大数据风险是恢复路径把不同 base manifest 的 target 当成同一 job 继续。
8. 最大运维风险是健康或元数据提交失败后二次补偿留下运行态与持久态分裂。

## 2026-07-30 任务 1：full rebuild source identity

- [x] 红测先加入 full 同路径 update、唯一纯 rename、unchanged 强制重建、
  新增/删除、重复摘要歧义、rename+update、hint 冲突、输入重排确定性和
  soft-route 旧 source ID；首次收集因缺少 `plan_full_rebuild` 退出 1。
- [x] 新 full planner 对同路径身份优先匹配，仅对两侧都唯一的内容摘要继承
  纯 rename；重复摘要和 rename+update 均不给 hint。所有 discovered 来源都
  生成 ADD 动作，不产生 UNCHANGED，因而每次 full 都重新解析、编码和写入。
- [x] `SyncAction.source_id_hint` 已纳入规范 plan digest 与 SQLite 计划持久化；
  旧 job_items schema 通过幂等加列迁移。StateStore 仅在 hint 格式合法、路径
  和既有 source ID 都无冲突时采用，否则 fail closed；新来源仍走原分配函数。
- [x] IndexCoordinator/SyncWorker 只把 planner 的 hint 传给 staging；
  IndexJobRunner 从活动 manifest 构造只含身份的 previous sources，未读取或
  复用旧 chunk，最终 manifest 路径、内容 SHA、doc version 和 source ID
  均来自新 target state。
- [x] 首轮实现回归为 `1 failed, 20 passed`，唯一差异是测试写反两个中文路径
  的稳定排序；修正预期顺序并提取 Ruff 指出的长度常量后，索引/状态专项为
  `21 passed, 5 warnings in 46.75s`。
- [x] 专项 Ruff `All checks passed!`、strict mypy `17 source files`、
  changed Google docstring `missing_google_sections=0` 和
  `git diff --check` 均退出 0；未修改 Parser、检索、生成或三份冻结配置。

## 2026-07-30 任务 2：incremental copy-on-write 发布

- [x] 真实 Qdrant 红测先证明旧实现把 incremental 复用 full collection，
  且 rename 成功、后续 update 失败后活动 payload 已被改写；首次为
  `2 failed, 2 warnings in 14.82s`。
- [x] full/incremental control job 现在都使用由 pipeline fingerprint 与
  job ID 确定的新物理 collection；incremental 从活动 manifest 冻结 alias、
  manifest digest、snapshot 名称/checksum、pipeline、source list 和 exact
  active count，发布前重新核对，任一漂移均失败关闭。
- [x] Qdrant clone 只接受活动 manifest 精确登记的 snapshot，恢复到不存在的
  target 后校验 dense/sparse/schema/pipeline 与 source/target exact active
  count；既有 target 仅在 control job、pipeline 和 base manifest 身份完全
  一致时恢复，否则拒绝。
- [x] SQLite collection state 使用只读源连接与 Python backup API 复制，
  校验 `integrity_check` 和 manifest exact source list，再以不覆盖 hard-link
  原子发布；未提交事务未进入 clone，目标 state 绑定同一 job/pipeline/base
  身份，旧 state 在任务路径中未写入。
- [x] 全部 ADD/UPDATE/RENAME/DELETE 仅作用于 target Qdrant 与 target state；
  所有 item 成功后统一构造完整 manifest，并复用
  snapshot → stage manifest → alias → activate manifest 发布事务，不再走
  same-collection `record_active_revision()`。
- [x] 真实故障矩阵覆盖两文档第二项失败、rename 成功/update 失败、delete
  成功/add 失败、snapshot 后 alias 前恢复、alias 后 manifest 前恢复、同一
  control job 发布后未 finish 的重领收敛，以及成功发布恰好一次 alias 切换。
  每个失败场景均验证旧 alias、active manifest、旧 Qdrant payload/count 和
  旧 SQLite source list 不变。
- [x] 成功路径逐项比较新 manifest 的 source ID/path/doc version 与 target
  exact active records，并真实切回旧 collection 后再恢复新 target，证明旧
  collection 可回滚；相关完整回归为
  `34 passed, 16 warnings in 117.44s`。
- [x] 专项 compileall 无输出、Ruff `All checks passed!`、strict mypy
  `10 source files`、Google docstring `missing_google_sections=0` 和
  `git diff --check` 均退出 0；未修改 Parser、检索、生成或三份冻结配置。

## 2026-07-30 任务 3：control/local lease heartbeat

- [x] 独立红测首次因 `rag_app.state.lease` 不存在而 collection error、退出 1；
  新 `LeaseHeartbeat` 不依赖第三方，使用非 daemon 线程、单调时钟和可中断
  Event 调度；间隔为租约四分之一且同时限制在 0.1–30 秒，并始终不超过
  `lease_seconds / 3`。
- [x] heartbeat 进入 context 时先用带时区 UTC 同步续租，后台异常只记录稳定
  失败状态，不保留或输出异常正文；`raise_if_failed()` 在主线程统一抛出
  `LEASE_LOST`。`close()` 可重复调用并 stop/join，正常、数据库异常和 owner
  被替换路径均验证无线程泄漏。
- [x] control heartbeat 覆盖从领取到 control `finish_job`；local heartbeat
  覆盖 SyncWorker 的完整 plan。单个 `build_chunks` 实际阻塞 1.2 秒、超过
  1 秒完整租约时，第二 worker 对 control 和 collection state 两层均无法领取；
  heartbeat 停止后按未来时间可由第二 worker 正常回收。
- [x] 主线程在每个 plan item 前后、snapshot clone 前后、所有 target Qdrant
  mutation 前后、create snapshot 前后、alias switch 前后、manifest activate
  前和两层 finish 前执行租约检查；长 item 内由后台线程续租，不使用无限 sleep。
- [x] control 续租数据库异常在 build 返回后的首个 mutation 前停止，control
  job 记录稳定 `LEASE_LOST`，target 保留 job/base staging 身份；旧 alias、
  active manifest 和旧 collection payload 不变。local 丢租约同样向 control
  提升为 `LEASE_LOST`，不继续发布。
- [x] publisher 在 snapshot 后 alias 前丢租约时不切换；alias 后 manifest 前
  丢租约时补偿恢复旧 alias，target manifest 保持 staging。正常 staged
  snapshot 恢复仍只切换一次 alias，同 job 重入契约未退化。
- [x] heartbeat、状态、worker、Qdrant、publisher 和 job runner 合并回归为
  `35 passed, 20 warnings in 159.03s`；专项 compileall 无输出、Ruff
  `All checks passed!`、strict mypy `6 source files`、Google docstring
  `missing_google_sections=0` 和 `git diff --check` 均退出 0。

## 2026-07-30 任务 4：deploy/rollback 完整事务

- [x] 静态事务红测首次为 `3 failed in 0.02s`，分别证明旧 deploy 没有
  candidate/active 分离与晚提交 rollback state、两脚本没有完整健康门槛、
  rollback 仍拒绝 degraded 当前运行态。
- [x] deploy 现在只接受固定
  `shared/env/candidates/<release-id>.env`，active 固定为
  `shared/env/rag.env`；两者必须是不同的 0600 普通文件、无 symlink 祖先。
  candidate revision、三镜像、release 和固定数据路径均在任何容器修改前校验；
  首次部署允许 active/current 不存在。
- [x] 升级前把 active env 原字节复制到 0600 临时快照，并核对 current
  revision、active 三镜像引用解析出的 image ID 与当前容器完全一致。候选中的
  非镜像配置刻意与 active 不同，所有故障补偿仍从原快照恢复，未从 candidate
  拼装旧配置。
- [x] 新 rollback state schema 记录上一 release、完整 env 的 base64 快照与
  SHA256、三个核心 image ID、worker existence/running/image 和 source
  revision；单一 0600 文件只在目标三容器 health、app `/live`、candidate
  env、current 和最终身份全部提交成功后原子替换。
- [x] deploy/rollback 共用有界健康语义：Qdrant、OCR、app 依次要求
  `.State.Health.Status=healthy`，`starting` 有固定 30 次上限，
  `unhealthy` 立即失败，缺容器/缺 health/无效值失败；app `/live` 使用连接与
  总超时重试，不要求 `/ready`。
- [x] rollback 不再要求当前核心全 running；调用前分别冻结 app/OCR/Qdrant/
  worker 的 existence、running 和 image。目标必须全健康，失败补偿则按服务
  精确恢复缺失、stopped 或 running，并恢复原 worker、active env 和 current；
  补偿自身失败稳定退出 70。
- [x] fake-command 反测覆盖成功、首次部署、load/部分 up/ps、三个 health、
  `/live`、active env/current/rollback state 三个提交点、旧 rollback state
  字节不变、starting→healthy、app stopped、OCR missing、三种 worker 状态及
  degraded 补偿；合并回归为 `69 passed in 11.31s`。
- [x] 两个 Shell 的 `bash -n`、专项 Ruff `All checks passed!` 与
  `git diff --check` 均退出 0；测试只使用临时目录和 fake command，未访问
  Docker daemon 或服务器。

## 2026-07-30 任务 5：wheel、OCI 与镜像归档身份闭环

- [x] 红测先以 `ModuleNotFoundError: rag_app.build_identity` 退出 1；新增
  tracked 开发占位 `_build_revision.py`，其值为 `development-unset`，不符合
  正式 40 位 revision 格式，因而不能冒充正式构建。
- [x] `prepare_runtime_wheels.py` 在任何下载/构建前要求含 untracked 文件在内
  的 clean Git，读取完整小写 HEAD，只复制 tracked 普通文件到临时源码树并在
  该副本写入 revision；真实源码占位值由测试逐字确认未变。项目 wheel 必须
  唯一，内嵌 revision 必须存在、格式正确且等于 HEAD。
- [x] wheelhouse 同步输出 `WHEELS.sha256` 和 `PROJECT_WHEEL.json`；后者只含
  schema、项目 wheel 名、wheel SHA256 与 source revision。缺 revision、
  大写/占位格式、旧 wheel 对新 HEAD、正确 wheel及 dirty Git 均有独立测试。
- [x] 应用 Dockerfile 在离线 pip install 与 pip check 之后导入已安装的
  `SOURCE_REVISION` 并与 `VCS_REF` 精确比较，不匹配使构建步骤失败；只读
  `build-info` 仅报告 installed revision、expected revision 和 matches，
  不输出路径、环境内容或 secret。
- [x] `IMAGE_ARCHIVES.tsv` 对 app、OCR、Qdrant 统一为 archive path、
  runtime tag、local image ID、source revision/批准 RepoDigest 四列；
  verifier 严格检查三行、顺序、四列和字段格式。deploy 在 load 后逐一检查
  三个实际 ID，并校验 app/OCR revision；rollback 也交叉核对三者身份。
- [x] fake Docker 故障矩阵中的 app、OCR、Qdrant load 后 ID 漂移都非零并
  完整恢复旧运行态；任务 5 合并回归为 `69 passed in 8.09s`，未构建镜像。
- [x] `compileall -q src tests scripts` 无输出、专项 Ruff
  `All checks passed!`、strict mypy `4 source files`、Google docstring
  `missing_google_sections=0`、四个相关 Shell 的 `bash -n` 与
  `git diff --check` 均退出 0。

## 2026-07-30 任务 6：外置 corpus manifest 与原子 package

- [x] package 契约红测先以 `2 failed in 0.02s` 证明旧脚本未要求外置
  manifest、仍硬编码语料数量/字节数且把双包分散写入 artifacts 根目录。
- [x] 新 manifest schema 固定 corpus ID、document count、total bytes、
  有序 path/size/SHA256 和整体 digest；JSON 必须 canonical。扫描递归处理
  DOCX，拒绝 root/成员 symlink、Zone.Identifier、越界路径与 case-fold
  冲突，并按相对 POSIX 路径排序。
- [x] freeze/verify/stage 使用窄职责模块且均少于 400 行；输出由操作员明确
  指定，默认建议目录已加入 `.gitignore`。测试覆盖 1、6、1000 份合成 DOCX，
  新增、删除、修改、额外 DOCX、symlink、Zone.Identifier、case-fold 冲突、
  路径越界和 manifest 篡改；未在进度或阻塞记录私有文件名。
- [x] package 现在强制绝对 `CORPUS_MANIFEST`，先校验 schema、corpus ID、
  exact DOCX set、逐文件 size/SHA 与整体摘要；不含真实文件名、固定数量或固定
  总字节数。复制严格按 manifest 顺序，并把原始 canonical manifest 放入
  corpus 包。
- [x] 全部 runtime、corpus、unpacker 与各自 sidecar 在
  `artifacts/releases/` 的同父目录隐藏 staging 中完成；两个内部 manifest
  先验证，成包后再安全解包验证外层 sidecar 与内部 exact manifest，并生成、
  复核 release 级 manifest。
- [x] Linux `renameat2(RENAME_NOREPLACE)` 只在同一真实父目录原子发布
  `<release-id>-<corpus-id>`，已存在目标和 rename 竞态均拒绝覆盖。runtime
  tar 成功后 corpus tar 失败、sidecar 失败时正式目录为零且 staging 被清理；
  竞态保留竞争方内容，成功仅出现七个完整 release 文件。
- [x] SBOM 前置、Qdrant canonical RepoDigest、三镜像四列白名单、
  unpacker sidecar 与双包内部 manifest 均保留；所有 package 测试只用小型
  合成输入、fake Docker/save/SBOM，未运行真实 package 或访问 daemon。
- [x] 任务 6 合并回归为 `89 passed in 10.34s`；`compileall` 无输出、
  Ruff `All checks passed!`、strict mypy `4 source files`、Google docstring
  `missing_google_sections=0`、五个 Shell 的 `bash -n` 和
  `git diff --check` 均退出 0。

## 2026-07-30 任务 7：不可变安装与备份身份

- [x] install/backup 契约红测先以 `4 failed in 0.05s` 证明 runtime 未包含
  原子安装器与 metadata helper、backup 未生成身份元数据且 app 恢复只请求
  `/live` 一次。
- [x] 新 `install.sh` 只接受安全解出的 runtime/corpus 绝对目录，再执行
  runtime `verify-offline.sh` 和 corpus 内部 manifest；拒绝输入/祖先 symlink、
  `.env.example` 以外的 env 文件、无效 ID、已存在目标、非 0600 外置 active
  env 和并发安装锁。
- [x] 安装先在 releases/corpora 各自同一父文件系统的隐藏目录复制；release
  所有目录设 0555、普通文件 0444、Shell 0555，corpus 目录/文件设
  0700/0400。两次发布均调用 `RENAME_NOREPLACE`；第二次发生竞态时只清理本
  事务已发布的 corpus，保留竞争方 release，且不留下伪完整 release/staging。
- [x] package 已把 install、原子 rename helper 和 backup metadata helper
  固化进 runtime，`verify-offline.sh` 将三者列为非空普通必需文件；公开部署
  手册已改为外置 corpus manifest、单一 release 输出目录和 `install.sh`
  安装，不再手工 `mv`。
- [x] `BACKUP_METADATA.json` 使用 canonical JSON，记录 schema、UTC 时间、
  current release ID、完整 source revision、app/OCR/Qdrant 实际 image ID、
  active env SHA256、state/Qdrant 归档名与 SHA；不含 env 内容或本地路径。
- [x] 写入服务停止后，metadata helper 以 SQLite `mode=ro` 和
  `query_only=ON` 尝试读取唯一 active manifest SHA/collection；缺失、schema
  不可读、多 active 或字段不安全时明确写 `null`，不修改数据库。
  Metadata 本身以 0600 纳入 `MANIFEST.sha256` 并在正式发布前再次复核。
- [x] app 恢复现在要求容器 running 且 `/live` 在最多 30 次内成功，每次有
  connect/总超时；前两次失败第三次成功后才恢复 worker，持续失败恰好请求
  30 次并稳定退出恢复错误。Qdrant 原有 bounded health 顺序保持不变。
- [x] Shell/fake-command 测试覆盖成功权限、输入保留、已有目标、unsafe env、
  release 竞态补偿、完整 metadata、真实只读 active manifest、metadata
  manifest 绑定、app 延迟恢复/超时及原服务集合；未执行服务器备份或部署。
- [x] 任务 7 合并回归为 `122 passed in 18.41s`；`compileall` 无输出、
  Ruff `All checks passed!`、strict mypy `5 source files`、Google docstring
  `missing_google_sections=0`、全部 deployment Shell 的 `bash -n` 与
  `git diff --check` 均退出 0。

## 2026-07-30 最终完整验收与边界审计

- [x] 唯一完整 pytest 验收退出 0：
  `499 passed, 45 warnings in 347.60s`，高于任务 0 的 419，skipped=0；
  warning 类别仍只有既有 `StarletteDeprecationWarning` 与 `UserWarning`，
  没有新增类别。
- [x] 指定静态门禁均退出 0：`compileall -q src tests scripts evaluation`
  无输出；全仓 Ruff `All checks passed!`；strict mypy
  `Success: no issues found in 96 source files`；默认 Google docstring
  `missing_google_sections=0`；全部 deployment Shell 的 `bash -n`、
  `git diff --check` 均无输出。
- [x] 默认 Compose 与 `--profile index` 两次 `config -q` 均退出 0；
  应用 `ASSETS.sha256` 11/11 逐项 `OK`。package 中固定语料数量/字节字面量
  为 0，新增 skip/xfail/todo 为 0。
- [x] 最终临时 Git index 发布安全扫描先发现两个、再发现一个测试凭据型
  字面量；改为明确 DUMMY/REPLACE 标记并跑定向测试后，最终为
  `tracked_files=236`、六类和总 `violations=0`，临时 index 已精确删除。
- [x] 保护摘要与任务 0 完全相同：docs `36c67e3…`、artifacts
  `220473c6…`、frozen `63adcd45…`、results `cdb17f0…`、evidence
  `05b845b9…`；pipeline/retrieval/corpus policy 分别为 `f61a74b0…` /
  `267e419f…` / `0d6553c1…`，retrieval 仍为 provisional。
- [x] 参考仓库仍 clean，HEAD/tree/tracked=182/聚合分别为
  `03d51db2…` / `84a0a960…` / `44254dff…`；当前仓库 HEAD 仍为
  `49c34074…`，范围外实现为 0，Parser/chunking/retrieval/generation/
  Query Trace 与三份冻结配置均未修改。
- [x] 真实 Git staged=0，`git write-tree` 与 `HEAD^{tree}` 都是
  `96df5fdd…`；但 `.git/index` 原始字节 SHA 为 `19f49405…`，不等于任务 0
  的 `dee80a74…`。无法从 SHA 恢复已丢失的原字节且未写回真实 index，已置顶
  记录到 `BLOCKED.md`；这是唯一新增的字面完成条件阻塞。
- [x] 本轮测试 Qdrant 在删除前复核为批准 v1.18.3 digest、mounts=[]、仅
  `127.0.0.1:6333`；只删除精确容器
  `rag-final-consistency-qdrant`，复核已不存在，未删除镜像、卷或网络。
- [x] 明确未执行 build/buildx、真实 image save/load、真实 package/正式双包、
  SSH/SCP、`.57/.58/.60`、服务器 backup/restore/rollback、commit/push/pull；
  所有部署、打包和备份动态测试均为临时目录加 fake command。

## 2026-07-30 白名单收敛修正

- [x] 重新逐字核对任务书后，发现此前新增的 corpus、原子目录、备份元数据和
  build identity 辅助模块不在精确白名单；已在继续验收前主动纠正，没有扩大
  修改范围。
- [x] corpus freeze/verify/stage 全部收敛到唯一允许新增的
  `scripts/freeze_corpus_manifest.py`；因禁止再拆分新模块，该文件现为 440 行，
  更正此前“各模块均少于 400 行”的记录，行为与安全断言未放宽。
- [x] `RENAME_NOREPLACE` 发布收敛到允许修改的 `scripts/offline_bundle.py`；
  backup metadata 收敛到允许修改的 `deployment/backup.sh` 内嵌只读 Python；
  build-info 收敛到允许修改的 `src/rag_app/cli.py`。
- [x] 删除范围外的 `scripts/{corpus_files.py,corpus_manifest.py,
  atomic_directory.py,backup_metadata.py}` 和
  `src/rag_app/build_identity.py`；`src/rag_app/state/__init__.py` 已恢复到 HEAD
  字节内容，lease/collection identity 改为从白名单模块直接导入。
- [x] 越界模块引用搜索为零；定向回归退出 0：
  `95 passed in 12.35s`。定向 Ruff 为 `All checks passed!`，mypy 为
  `Success: no issues found in 5 source files`，Google docstring 为
  `missing_google_sections=0`，相关 Shell `bash -n` 与 `git diff --check`
  均退出 0。
- [x] 本次修正仍未执行 commit/push；当前 `/goal` 明确将二者列为禁止操作。

## 2026-07-30 白名单收敛后的完整验收红证据

- [ ] 第二轮完整 pytest 首次运行真实退出非零，摘要为
  `2 failed, 497 passed, 45 warnings in 368.72s`；失败分别是 policy change
  full rebuild 和 registered snapshot clone，均发生在本地 Qdrant HTTP 调用。
- [x] SQLite 只读复核显示前者稳定记录
  `INDEX_RESPONSEHANDLINGEXCEPTION`；后者 traceback 为
  `qdrant_client.http.exceptions.ResponseHandlingException: timed out`。
  Qdrant 容器仍 running、OOMKilled=false、mounts=[]、仅绑定
  `127.0.0.1:6333`，日志无 panic/OOM，故没有把该红证据误写成绿。
- [x] 运行环境同时注入了 localhost HTTP proxy 与非标准 `NO_PROXY=127.*`；
  traceback 实际经过 `http_proxy.py`，Qdrant 日志对应请求存在约 10 秒空档。
  下一步先清除代理变量并对两个失败用例做定向复核，不直接重跑全量碰运气。

## 2026-07-30 白名单收敛最终验收

- [x] 清除测试进程的代理变量后，两个原失败用例定向复核退出 0：
  `2 passed, 2 warnings in 23.28s`；随后使用最后一轮完整验收额度，真实退出码
  为 0，摘要为 `499 passed, 45 warnings in 343.67s`，skipped=0，warning
  类别仍只有既有 Starlette deprecation 与本地 Qdrant API-key 提示。
- [x] 最终静态门禁均退出 0：compileall 无输出，Ruff
  `All checks passed!`，strict mypy
  `Success: no issues found in 91 source files`，Google docstring
  `missing_google_sections=0`，全部 deployment Shell、默认/index profile
  Compose 和 `git diff --check` 均通过。
- [x] 资产 SHA 首次从错误的 `deployment/` 工作目录执行，因 manifest 路径按
  仓库根解释而 11 项均报告无法打开；从仓库根按指定命令纠正后 11/11 全部
  `OK`，没有修改 manifest 或资产。
- [x] 最终临时 Git index 纳入全部候选后为 `tracked_files=231`，binary、
  large、local path、private network、private path、secret 和总
  `violations` 均为 0；临时 index 已精确删除，真实 index 扫描前后均为
  `3533a1c7…`。
- [x] 保护摘要仍为 docs `36c67e3b…`、artifacts `220473c6…`、frozen
  `63adcd45…`、results `cdb17f0c…`、evidence `05b845b9…`；三份冻结配置为
  `f61a74b0… / 267e419f… / 0d6553c1…`。参考仓库仍 clean，
  HEAD/tree/tracked 为 `03d51db2… / 84a0a960… / 44254dff…`。
- [x] 当前 HEAD 仍为 `49c34074…`，`git write-tree` 与 `HEAD^{tree}` 均为
  `96df5fdd…`，staged=0、deleted=0；当前修改 38 个 tracked 文件，新增
  15 个文件，全部命中精确白名单。
- [x] 临时 Qdrant 删除前为批准 v1.18.3 digest、running、OOMKilled=false、
  mounts=[] 且仅本机回环端口；已精确删除
  `rag-final-consistency-qdrant` 并复核不存在，镜像仍在，未删除镜像、卷或网络。
- [x] 最终仍未执行 build/buildx、真实 image save/load、真实 package/正式双包、
  SSH/SCP、服务器访问/部署/backup/restore/rollback、commit/push/pull。

## 2026-07-30 用户覆盖提交边界

- [x] 完整交付与阻塞说明后，用户再次明确要求“将代码 commit 并 push”，该指令
  覆盖本轮任务书中仅针对 Agent 的 commit/push 禁令，并接受已如实记录的真实
  Git index 原始字节 SHA 差异；不覆盖 build、package、服务器访问或部署禁令。
- [x] 提交范围仍只包含本轮精确白名单内的索引事务、租约、部署事务、构建身份、
  corpus manifest/原子发布、对应测试及本进度与阻塞记录。

## 2026-07-30 四项生产发布风险任务 0：当前 HEAD 基线

- [x] 起始 HEAD 为 `caf5bdba83c149845bc1f0e48d1dc8f3491fbe1c`，
  本地 `main` 与 `origin/main` 同步；tracked=231，工作树、staged 和 deleted
  均为 0。Python 为 3.11.15，Docker Engine 29.4.0、Compose 5.1.2。
- [x] 三项逻辑 Git index 不变量通过：`git diff --cached --quiet` 退出 0，
  `git write-tree` 与 `HEAD^{tree}` 均为
  `d59ea6c35c3c8c7409300b851e6caf0f26497367`，staged=0。原始
  `.git/index` SHA 仅保留在 `BLOCKED.md` 历史审计，不再作为发布门禁。
- [x] 保护摘要未漂移：docs `36c67e3b…`、artifacts `220473c6…`、
  frozen `63adcd45…`、results `cdb17f0…`、evidence `05b845b9…`；
  pipeline/retrieval/corpus policy 分别为 `f61a74b0…` / `267e419f…` /
  `0d6553c1…`，retrieval 继续为 provisional。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked 聚合为
  `03d51db2…` / `84a0a960…` / `44254dff…`。
- [x] 本地已有固定 `qdrant/qdrant:v1.18.3` amd64 镜像，使用
  `--pull never` 启动精确测试容器 `rag-release-risk-qdrant`；
  `mounts=[]`、仅绑定 `127.0.0.1:6333`、OOMKilled=false。
- [x] 当前 HEAD 全量 pytest 为
  `499 passed, 45 warnings in 351.42s`，skipped=0；warning 类别仍只有
  Starlette deprecation 和本地 Qdrant API-key/兼容性提示。
- [x] compileall 无输出、Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 91 source files`、Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、11/11 ASSETS 和 `git diff --check` 均退出 0。
- [x] 临时 Git index release-safety 为 `tracked_files=231`，binary、
  large、local path、private network、private path、secret 和总 violations
  均为 0；临时 index 已精确删除。
- [x] 当前代码审计确认本轮四个缺口仍存在：无统一发布前 target verifier、
  无 `index-gc`、安装不能独立幂等复用 runtime/corpus、wheelhouse 三件套
  不是事务替换且 serve/worker/index 未在外部资源前强制 revision 相等。
- [x] 本阶段未执行 build/buildx、image save/load、真实 package、联网安装、
  SSH/SCP、`.57/.58/.60`、服务器操作、commit 或 push。

## 2026-07-30 四项生产发布风险任务 1：target 全量一致性证明

- [x] 红测首次在收集阶段退出 1：两个测试模块均报
  `ModuleNotFoundError: rag_app.index.verifier`。新增测试覆盖真实 Qdrant
  删除点、额外 active 点、错误 chunk_count、残留 staging、损坏 SQLite、
  verifier 重入及 job runner 必须在 snapshot 前调用 verifier。
- [x] 新 `TargetIndexVerifier` 先以只读 SQLite
  `PRAGMA integrity_check` 校验 state，再核对 control job、pipeline、base
  manifest 三方身份和待发布 manifest 的 exact active source 列表。
- [x] 每个 manifest `source_id+doc_version` 必须在 SQLite 为 active、具有
  正 chunk_count 且 Qdrant exact count 相等；target 总 active 点数必须等于
  全部 chunk_count 之和，并要求 staging 点为 0。因此删除点、额外 active
  source/version 及相互抵消的漂移都会失败关闭。
- [x] Qdrant 兼容检查补齐 dense cosine distance、BM25 sparse IDF、
  collection/payload schema、全部固定 payload index 类型及 index revision；
  pipeline fingerprint 继续包含冻结 index revision，未改三份 deployment
  config。
- [x] 既有 full target 恢复要求 collection/state 同时存在，先验证 Qdrant
  结构与 staging identity，再以只读方式验证 SQLite integrity/identity；
  incremental 已有 state 还必须与 base manifest 来源完全一致，不能重新
  initialize 或补写身份来掩盖损坏。
- [x] publisher 通过独立 target guard 在 snapshot、stage manifest、alias
  和 activate manifest 前重复执行统一 verifier；同 control job 已发布重入
  也会重新验证完整 target。反测证明 verifier 失败时 snapshots=[]、
  target manifest 不存在且 alias 未切换。
- [x] 首轮合并回归为 `1 failed, 39 passed`；唯一失败揭示纯 rename 后
  immutable source version 保留初始路径，而 active source 使用当前路径。
  verifier 改为以活动来源表验证 current path，不错误要求历史 version path
  改写；没有改变 rename 或检索行为。
- [x] 最终 target/Qdrant/state/publisher/job runner/runtime 合并回归为
  `60 passed, 31 warnings in 178.15s`。专项 compileall 无输出、Ruff
  `All checks passed!`、strict mypy `7 source files`、Google docstring
  `missing_google_sections=0` 和 `git diff --check` 均退出 0。

## 2026-07-30 四项生产发布风险任务 2：保守索引 GC

- [x] 红测首次在收集阶段退出 1：
  `ModuleNotFoundError: No module named 'rag_app.index.gc'`。测试覆盖默认
  dry-run、显式 apply、幂等重跑、活动 alias、最新两份 retired、失败任务、
  orphan target、未知 collection、state、snapshot、任务并发、alias 漂移和
  collection 删除失败后的重试。
- [x] `index-gc` 仅把可验证为兼容索引且具有受管 staging identity 的目标纳入
  collection 回收；保护 alias、active/staging manifest、最新两份 retired、
  非失败任务 target 和未知/不兼容 collection。对应 SQLite 必须为普通非
  symlink 文件、完整且无本地 pending/running 任务。
- [x] snapshot 只有在当前及历史 manifest 均无引用时才进入计划；dry-run
  零写入，`--apply` 在每个对象前后复核 alias、manifest、snapshot 引用和
  control job 快照。控制面漂移立即拒绝继续。
- [x] apply 按 snapshot、collection、state 顺序执行并逐项给出稳定状态；
  collection 删除失败时保留 state，后续重跑可恢复；已不存在对象返回
  `already_absent`，因此成功计划可幂等重放。CLI 输出不含正文、本地路径或
  配置内容。
- [x] 首轮专项测试为 `5 passed, 4 warnings in 130.00s`。首次静态复核真实发现
  Ruff 5 项复杂度/性能问题和 mypy 1 项联合类型问题；通过冻结配置对象及窄职责
  helper 修正后，Ruff `All checks passed!`、strict mypy
  `Success: no issues found in 3 source files`、compileall 无输出且
  `git diff --check` 退出 0。
- [x] 与 target verifier、job runner、Qdrant、state、publisher 和 runtime
  的真实 Qdrant 合并回归为
  `53 passed, 34 warnings in 316.24s`；skipped=0，warning 均为已有本地
  HTTP API-key 提示。本阶段未 build、package、访问服务器或改动冻结检索配置。
- [x] 推送前唯一全量 pytest 退出 0：
  `515 passed, 60 warnings in 498.88s`，skipped=0；warning 类别仍只有已有
  Starlette deprecation、本地 Qdrant API-key 和兼容性提示。全量 compileall
  无输出、Ruff `All checks passed!`、mypy
  `Success: no issues found in 93 source files`、Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose 与 `git diff --check` 均退出 0。

## 2026-07-30 四项生产发布风险任务 3：runtime/corpus 独立安装

- [x] 新增契约与 fake-command 红测首次为 `6 failed, 5 passed in 3.79s`，
  真实证明旧安装器没有 root 门禁、复制后 runtime 复核、corpus manifest
  语义校验、owner 设置和既有 runtime/corpus 幂等复用。
- [x] `install.sh` 现在先以 `/usr/bin/id -u` 要求 root，再验证输入 runtime
  与 corpus；复制到同父目录 staging 后重新运行 `verify-offline.sh`，
  重新校验 corpus `MANIFEST.sha256`、canonical
  `CORPUS_MANIFEST.json`、DOCX exact set 与 `CORPUS_ID`。
- [x] corpus staging 在发布前递归设为 `10001:10001`，目录严格 0700、文件
  严格 0400，并在原子 rename 前复核 owner/权限。打包与离线 verifier 已把
  `freeze_corpus_manifest.py` 固化为 runtime 必需普通文件，安装后不再依赖
  仓库或联网资源。
- [x] 既有 release 只有在 `SOURCE_REVISION`、`MANIFEST.sha256` 和精确文件
  集合与输入一致且自身验证通过时才只读复用；既有 corpus 同样要求两份
  manifest、精确文件集合、内容、owner 和权限全部一致。允许复用同一 runtime
  安装新 corpus，也允许完全相同的两者幂等重跑；任一漂移均拒绝且不修改目标。
- [x] 安装锁只由实际持有者清理；复制后篡改会在发布前失败，release rename
  竞态只补偿删除本事务刚发布的 corpus，不删除既有 release/corpus，且不残留
  staging。手册已改为 `sudo bash install.sh` 并删除手工修 corpus owner 的步骤。
- [x] 最终安装、package 与 offline bundle 合并回归为
  `33 passed in 9.51s`；相关 Ruff `All checks passed!`，三个 Shell
  `bash -n` 与 `git diff --check` 均退出 0。本阶段未执行真实 package、
  build、联网、服务器访问、commit 或 push。

## 2026-07-30 四项生产发布风险任务 4：wheelhouse 与运行身份事务

- [x] wheelhouse 事务红测覆盖 download、build、manifest 写入、metadata
  写入和移动失败。旧实现中 manifest/metadata 失败后旧 wheel 已被替换，
  move 故障注入未被调用；运行时红测则因不存在可校验的
  `runtime.SOURCE_REVISION` 入口，在 setup 阶段稳定报错。合并红测摘要为
  `3 failed, 10 passed, 28 errors in 1.76s`。
- [x] `prepare_runtime_wheels.py` 现在强制 wheelhouse、
  `WHEELS.sha256` 与 `PROJECT_WHEEL.json` 位于同一真实父目录；所有下载、
  项目 wheel 构建、revision、逐 wheel SHA 和项目元数据均先在隐藏 staging
  完整生成并再次验证，staging 只允许非空普通 `.whl` 文件集合，正式三件套
  在此之前保持不变。补充反测先以 `1 failed, 14 deselected` 证明意外非 wheel
  文件曾被忽略，修复后 wheel 事务全集为 `15 passed in 0.20s`。
- [x] 替换时每个旧对象只原子移动到同文件系统事务备份，不先删除旧 wheel；
  任一移动失败会逆序移回已安装新对象并恢复全部旧对象。测试逐字节比较旧
  wheel 文件集合、清单和元数据，五类失败均保持三者完全不变；成功更新一次
  替换三者且不残留隐藏 staging/backup。
- [x] `require_release_revision()` 已成为 `build_runtime()` 与
  `build_worker_runtime()` 的第一项检查；`serve`、`worker`、一次性 `index`
  因而会在读取 pipeline、创建 Qdrant、SQLite 或 HTTP 客户端前要求安装 wheel
  的 `SOURCE_REVISION` 为 40 位小写 Git SHA 且精确等于
  `RuntimeSettings.release_revision`。空值、`development-unset` 和错配均失败，
  三个 CLI 路径的外部资源调用计数均为 0；`build-info` 行为未改。
- [x] 任务 4 专项回归为 `42 passed, 2 warnings in 3.34s`；与 runtime、
  build identity、install、package、offline bundle 的合并回归为
  `88 passed, 2 warnings in 12.02s`。相关 Ruff、strict mypy
  `3 source files`、Google docstring、三个 Shell 和 `git diff --check`
  均退出 0；未真实下载、构建 wheel、package、联网或访问服务器。

## 2026-07-30 四项生产发布风险最终完成审计

- [x] 逐条审计任务 0-4：Git 发布口径只剩三项逻辑 index 不变量；target
  verifier、保守 `index-gc`、runtime/corpus 独立安装、wheelhouse 三件套事务
  和启动 revision 预检均有对应实现、红证据与故障反测。没有用窄测试代替本节
  的全量验收。
- [x] 唯一最终全量 pytest 退出 0：
  `536 passed, 60 warnings in 544.84s`，skipped=0；warning 类别仍只有既有
  Starlette deprecation、本地 Qdrant API-key 与客户端兼容性提示，没有新增
  warning 类别。
- [x] 全量 compileall 无输出、Ruff `All checks passed!`、mypy
  `Success: no issues found in 93 source files`、Google docstring
  `missing_google_sections=0`、全部 deployment Shell 和 index profile
  Compose 均退出 0。默认 Compose 与最终 `git diff --check` 首次并行调用遇到
  WSL 服务层 `E_UNEXPECTED/0x8007274c`、没有产生工具结果；串行复跑两者均真实
  退出 0。
- [x] `deployment/ASSETS.sha256` 11/11 全部 `OK`。临时 Git index 扫描为
  `tracked_files=235`，binary、large、local path、private network、
  private path、secret 与总 `violations` 均为 0；精确临时 index 已删除并
  复核不存在。
- [x] 三项逻辑 Git index 不变量最终通过：`git diff --cached --quiet`
  退出 0，`git write-tree` 与 `HEAD^{tree}` 均为
  `fd24dd40d9814766e8f43cc06a59aaf517d24f15`，staged=0。`.git/index`
  原始字节 SHA 只保留历史审计，不是 blocker，也未尝试恢复。
- [x] 保护摘要与任务 0 相同：docs `36c67e3b…`、artifacts `220473c6…`、
  frozen `63adcd45…`、results `cdb17f0c…`、evidence `05b845b9…`；
  pipeline/retrieval/corpus policy 分别为
  `f61a74b0… / 267e419f… / 0d6553c1…`，三份配置无 diff，retrieval 继续为
  provisional。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked 聚合仍为
  `03d51db2… / 84a0a960… / 44254dff…`。当前修改仅有白名单内 13 个文件，
  deleted=0，新增 skip/xfail/TODO=0；Parser、chunking、检索、rerank、Prompt、
  回答 schema、模型参数与三份冻结配置均未修改。
- [x] 最终测试容器删除前复核为 `rag-release-risk-qdrant`、running、
  OOMKilled=false、mounts=[] 且仅绑定 `127.0.0.1:6333`；已只删除该精确
  容器并复核不存在，未删除镜像、卷或网络。
- [x] 本次任务 3/4 续跑未执行 build/buildx、image save/load、真实
  package/双包、联网安装、SSH/SCP、`.57/.58/.60`、服务器操作、commit 或
  push。任务 0-2 的三笔提交/推送发生在本次续跑前，来源于用户明确覆盖指令，
  已在上文“用户覆盖提交边界”保留审计；本次工作区改动保持 unstaged。
- [x] `BLOCKED.md` 继续保留真实模型消融/revision、Word 自动编号、GPU OCR、
  EMF 转换器、完整 chat-template token 预算、正式离线双包与服务器验收，
  本轮没有伪造或提前解除这些外部证据项。

## 2026-07-30 首次部署最后三项任务 0：新 HEAD 基线

- [x] 本轮恢复时当前工作树已由外部变为 clean，HEAD 为
  `1a4d158974f41aa79f11e78c3d9c2902c8db89e9`，最近四笔提交为上一轮
  install、wheel、runtime revision 与文档改动；本轮不改写这些历史。
- [x] 三项逻辑 Git index 不变量通过：`git diff --cached --quiet` 退出 0，
  `git write-tree` 与 `HEAD^{tree}` 均为
  `6d3ba0a680b89cd32a293ca7ea45a6a5f4521920`，staged=0。
- [x] 本地固定 Qdrant 镜像仍为批准 digest
  `sha256:0bd98fa…`、`amd64/linux`；确认 6333 未占用后，以 `--pull never`
  启动精确测试容器 `rag-final-three-qdrant`，无挂载且只绑定
  `127.0.0.1:6333`，`/readyz` 返回 `all shards are ready`。
- [x] 当前 HEAD 全量 pytest 退出 0：
  `536 passed, 60 warnings in 529.92s`，skipped=0；warning 类别仍只有既有
  Starlette deprecation、本地 Qdrant API-key 与客户端兼容性提示。
- [x] compileall 无输出、Ruff `All checks passed!`、mypy
  `Success: no issues found in 93 source files`、Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、11/11 ASSETS 与 `git diff --check` 均退出 0。
- [x] 临时 Git index release-safety 为 `tracked_files=235`，binary、large、
  local path、private network、private path、secret 与总 `violations`
  均为 0；精确临时 index 已删除并复核不存在。
- [x] 保护摘要与上一任务一致：docs `36c67e3b…`、artifacts `220473c6…`、
  frozen `63adcd45…`、results `cdb17f0c…`、evidence `05b845b9…`；
  pipeline/retrieval/corpus policy 分别为
  `f61a74b0… / 267e419f… / 0d6553c1…`，retrieval 继续为 provisional。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked 聚合仍为
  `03d51db2… / 84a0a960… / 44254dff…`。本阶段未 build、save/load、
  真实 package、联网安装、访问服务器、commit 或 push。

## 2026-07-30 首次部署最后三项任务 1：健康等待契约

- [x] 先增加 fake clock 与 deadline 契约测试；旧实现真实红灯为
  `5 failed, 7 passed`：deploy/rollback 都仍是 30 次固定循环，OCR 在
  fake 第 31、90、210 秒才健康时均误判失败，静态契约也找不到四类
  显式 timeout。
- [x] `deployment/deploy.sh` 与 `deployment/rollback.sh` 现使用相同常量：
  Qdrant 60 秒、app health 60 秒、app `/live` 60 秒、OCR 240 秒；循环按
  wall-clock deadline 和剩余时间休眠，不再使用 `max_attempts=30`。
- [x] 两条主路径与失败补偿均复用同一 health helper/timeout。`starting`
  继续等待，`healthy` 成功，`unhealthy`、health inspect 失败/空字段、
  检查中容器消失立即失败；仍只检查 `/live`，没有增加 `/ready`。
- [x] fake docker/sleep/date 不发生真实等待；行为反测覆盖 OCR 第 31、
  90、210 秒成功，第 240 秒持续 starting 超时并恢复旧运行态，以及
  unhealthy、无 health、容器消失和 rollback/deploy 补偿路径。
- [x] 修复后定向契约为 `15 passed, 57 deselected in 13.25s`；完整
  deploy/rollback 相关套件为 `72 passed in 31.44s`。对应 Ruff
  `All checks passed!`、两份 Shell `bash -n` 与 `git diff --check`
  均退出 0。本阶段未 build/package、联网、访问服务器、commit 或 push。

## 2026-07-30 首次部署最后三项任务 2：release 不可变

- [x] 先把 runtime/corpus 输入在 fakeroot 中明确设为普通用户
  `1234:1234`，再增加发布 owner 与既有 release 漂移反测；旧实现真实红灯
  为 `6 failed, 9 deselected`：发布目标仍继承 `1234:1234`，owner、
  普通文件 0644、Shell 0544、目录 0755 漂移均被错误复用。
- [x] 新 release staging 在身份、MANIFEST、复制文件集复核后执行
  `chown -R root:root`；所有目录与 `*.sh` 固定 0555，其余普通文件固定
  0444，并逐项拒绝特殊文件、非 root owner/group 或任一 mode 偏差。
- [x] 既有 release 仍先复核 SOURCE_REVISION、MANIFEST、输入/目标文件集，
  再全量验证 root:root 与精确 mode；发现漂移直接退出，反测确认失败后
  `1234:1234/0644/0544/0755` 原值未被静默修复。
- [x] 原有复制后 runtime/corpus 篡改与发布竞态仍通过；corpus 保持
  `10001:10001`、目录 0700、文件 0400。完整 install 套件为
  `15 passed in 14.08s`，对应 Ruff `All checks passed!`、`bash -n
  deployment/install.sh` 与 `git diff --check` 均退出 0。本阶段未
  build/package、联网、访问服务器、commit 或 push。

## 2026-07-30 首次部署最后三项任务 3：index-gc 收紧

- [x] 先增加 revision/缺库顺序、只读连接、dry-run 文件集与摘要、只读文件
  系统、WAL/SHM、sidecar symlink、部分删除、collection/state 同名替换
  反测；旧实现真实红灯为 `9 failed, 1 passed`。其中 dry-run 会创建/修改
  SQLite sidecar，既有 state apply 只删除主库且会删除同名替换对象。
- [x] `index-gc` 现在首先执行 `require_release_revision()`，随后要求
  control/manifest 主库均已存在、为非 symlink 普通文件；revision 错配、
  主库缺失及两类 symlink 测试都证明在 pipeline、Qdrant 与 SQLite 前失败，
  且主库、WAL、SHM 均未创建。
- [x] GC 不再注入可写 `ManifestRepository/StateStore`。只读入口先按字节
  冻结并稳定复制主库与已提交 WAL 到自动清理的隔离目录，再仅对副本使用
  `mode=ro + query_only`；这避免源 WAL 模式数据库因只读 SELECT 创建 SHM，
  又不会像 `immutable=1` 一样忽略未 checkpoint WAL。专门反测确认非空 WAL
  中的新 identity 可见，源主库/WAL/SHM 文件集和 SHA256 前后完全不变。
- [x] CLI 还会在完整 plan 前后复核 control、manifest 与全部 collection
  state 三件套的文件集和 SHA256；真实 Qdrant + SQLite dry-run 在普通与
  0555/0444 只读树上均零变化。
- [x] plan 冻结 Qdrant staging identity 与 collection state identity；
  apply 在 collection 删除前重新验证二者，collection 或 state 同名替换均
  返回 `identity_changed` 且保留替换对象。意外编程/控制漂移异常不再被
  broad `except Exception` 吞掉，仅已知 Qdrant API/OS 删除失败转为稳定
  `delete_failed`。
- [x] state 主库、`-wal`、`-shm` 作为逻辑集合：collection 成功删除后才
  处理；WAL/SHM 任一 symlink 返回 `unsafe_state`；全缺失为
  `already_absent`；任一 unlink 失败或有残留均不报告 deleted；重复 apply
  幂等。
- [x] 新安全套件为 `13 passed, 1 warning in 33.77s`；旧 GC 的 apply
  幂等与 collection 删除失败关键回归为 `2 passed, 2 warnings in
  64.61s`。专用本地真实 Qdrant 客户端设置 `trust_env=False`，避免当前
  WSL 的 `NO_PROXY=127.*` 被 HTTPX 忽略后误走 7890 代理；没有 mock
  Qdrant。对应 compileall、Ruff、mypy、`git diff --check` 均退出 0。
  本阶段未 build/package、联网、访问服务器、commit 或 push。

## 2026-07-30 首次部署最后三项最终验收与提交授权

- [x] 唯一有效的最终全量 pytest 退出 0：
  `562 passed, 61 warnings in 542.05s`，skipped=0；高于任务 0 的
  536 passed。warning 类别仍只有既有 `StarletteDeprecationWarning`
  与 `UserWarning`，没有新增类别。
- [x] 最终静态门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 93 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、`git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] 最终临时 Git index release-safety 为 `tracked_files=236`，binary、
  large、local path、private network、private path、secret 与总
  `violations` 均为 0；临时 index 已精确删除。第一次 PowerShell→WSL
  变量传递错误产生的 `tracked_files=0` 结果明确作废，没有当作门禁证据。
- [x] 保护摘要与任务 0 完全一致：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`，
  retrieval 继续为 provisional。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked=182/聚合分别为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`。
  新增 skip/xfail/TODO=0；Parser、chunking、检索、rerank、Prompt、回答
  schema、模型参数与三份冻结配置均无修改。
- [x] 测试后专用 `rag-final-three-qdrant` 仍 running、OOMKilled=false、
  RestartCount=0、mounts=[]、仅绑定 `127.0.0.1:6333`，collection 列表为空。
  本轮没有 build/buildx、image save/load、真实 package/双包、联网安装、
  SSH/SCP、`.57/.58/.60` 或服务器操作。
- [x] 用户在全部实现完成后最新明确要求“将代码 commit 并 push”，因此仅覆盖
  本任务书的 commit/push 禁令；不扩大到 build、package、服务器或其他联网
  操作。提交前仍要求真实 index staged=0、白名单 diff 与发布安全扫描全绿。
- [x] `BLOCKED.md` 继续保留真实模型消融/revision、Word 自动编号、GPU OCR、
  EMF、完整 chat-template token 预算、正式离线双包与服务器验收；这些外部
  证据项没有被本轮源码门禁误报为已完成。

## 2026-07-30 首次部署入口收口任务 0：基线

- [x] 当前 HEAD 为
  `2aa972560ddc8300b9d901835031f814aac7a58a`，`main` 与
  `origin/main` 无 ahead/behind，工作树与暂存区均为空。`git write-tree`
  与 `HEAD^{tree}` 均为 `3c1f07178bf6dc688138494d0017a8737e9e5289`。
- [x] 当前 HEAD 唯一全量 pytest 基线退出 0：
  `562 passed, 61 warnings in 554.92s`，skipped=0；warning 类别仍只有
  `StarletteDeprecationWarning` 与 `UserWarning`。
- [x] 快速门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 93 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、`git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] release-safety 为 `tracked_files=236`，binary、large、local path、
  private network、private path、secret 与总 `violations` 均为 0。
- [x] 保护摘要继续为 docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`，
  retrieval 继续为 provisional。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked=182/聚合分别为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`。
- [x] 专用本地 Qdrant 在测试前后均 running、无挂载、仅绑定
  `127.0.0.1:6333`，collection 列表为空；本阶段未 build、save/load、
  package、联网安装、访问 `.57/.58/.60`、commit 或 push。

## 2026-07-30 首次部署入口收口任务 1：候选 env 文档契约

- [x] 新静态契约先在旧文档上真实红灯：
  `1 failed, 3 deselected in 0.03s`。两份公开文档均未创建 0700
  `shared/env/candidates`，且都把 active `rag.env` 直接传给只接受
  `candidates/<release-id>.env` 的 `deploy.sh`。
- [x] `deployment/README.md` 与公开离线部署手册现在都明确区分首次部署和
  升级：首次部署以 0600 从 release `.env.example` 安装候选文件；升级先
  拒绝覆盖同名候选，再从 active `rag.env` 复制并 chmod 0600；两条路径都只
  编辑并传递 `candidates/${release_id}.env`。
- [x] 两份文档均明确 active `rag.env` 只能由 deploy 成功后发布；后续
  Compose、backup 和 rollback 继续读取固定 active env。静态反测同时验证
  deploy/rollback 脚本参数契约，并禁止任何 `deploy.sh .../rag.env` 命令。
- [x] 完整静态契约为 `4 passed in 0.02s`，Ruff
  `All checks passed!`，`git diff --check` 退出 0。首次并行完整测试遇到
  WSL 宿主 `0x8007274c`、没有 pytest 结果，串行复跑真实通过；本阶段未
  build/package、联网、访问服务器、commit 或 push。

## 2026-07-30 首次部署入口收口任务 2：Qdrant 语义就绪

- [x] 修正 fake f-string 转义后，旧实现的有效红测为
  `10 failed, 2 passed, 51 deselected in 3.75s`：即使 Qdrant 端口 health
  已 healthy，`/readyz` 延迟、连接失败、非 200、超时或容器消失仍会错误
  提交 env/current。更早一轮 deploy 夹具 `NameError` 明确作废，没有用作
  产品红证据。
- [x] deploy/rollback 均新增独立 60 秒 `/readyz` deadline。每次请求都由
  `docker exec rag-app python -c` 在容器内执行，从容器环境读取
  `RAG_QDRANT_URL` 与 `RAG_QDRANT_API_KEY`，请求 timeout 不超过当前 deadline
  剩余时间；只有 HTTP 200 成功。
- [x] 连接失败、非 200 与单次超时只在 deadline 内重试；rag-app 或
  rag-qdrant 消失立即失败。Python stdout/stderr 全部丢弃，Shell 只输出通用
  失败分类，不记录 API key、URL 响应正文或状态正文，也没有发布 Qdrant
  宿主端口。
- [x] deploy 主路径与旧 runtime 补偿复用完整健康 helper；rollback 主路径
  同样复用，rollback 独立补偿在原 app 与 Qdrant 都应运行时再次执行语义
  readiness。目标 readiness 失败后，fake 日志证明旧 runtime readiness
  也被执行后才报告恢复成功。
- [x] 新定向矩阵为 `13 passed, 54 deselected in 12.00s`；deploy、rollback
  和静态契约完整回归为 `67 passed in 31.46s`。相关 Ruff
  `All checks passed!`、两份 Shell `bash -n` 与 `git diff --check` 均退出
  0；两份部署文档已同步说明端口 health 与容器内语义 readiness 的区别。
  本阶段未 build/package、联网、访问服务器、commit 或 push。

## 2026-07-30 首次部署入口收口任务 3：部署状态机

- [x] 状态矩阵先在旧实现上真实红灯 `10 failed, 61 deselected in 2.45s`：
  fresh 会沿用 stale rollback；degraded 不发布完整 rollback；active/current
  缺一和旧 worker image 错配仍会进入 load；旧 release 未在部署或 rollback
  补偿前复验。rollback worker 错配用例修正“保持测试输入不变”的断言后，
  进一步证明旧实现已执行 compose up 才失败。
- [x] deploy 在第一条 `docker load` 前只读收集 active env、current、三个
  核心容器、worker 和 rollback state 的存在性，并唯一分类为：
  fresh（五类均无）、installed（合法 active/current + 三核心完整）、
  degraded（合法 active/current + 核心全无，worker 可无或合法）或 invalid。
  任何其他组合均立即退出。
- [x] fresh 遇到任意旧 rollback state 直接拒绝。installed/degraded 均要求
  current 直接指向安全旧 release、active revision 匹配，并重新执行旧 release
  `verify-offline.sh`；active 三镜像解析为实际 image ID，若旧 worker 存在，
  其 image 必须精确等于旧 app image。
- [x] degraded 无论无 worker 或仅有合法旧 worker，成功升级都会从 active
  env/current/旧镜像生成 schema v2 完整 rollback state；部署失败补偿仍精确
  恢复“核心全无”和原 worker 状态，不把 degraded 误恢复成 installed。
- [x] deploy 在进入失败补偿前再次复验旧 release；专门反测确认第一次复验
  位于 load 前，第二次位于故障后的补偿前。rollback 继续先复验 rollback
  target，同时在任何 runtime 变更前复验 current/original release，并在补偿
  前再次复验；rollback state 中 worker image 与 app image 不同会在 up 前拒绝。
- [x] 状态矩阵定向修复后为 `11 passed, 60 deselected in 1.63s`，补偿复验
  补充为 `4 passed, 68 deselected in 3.95s`；deploy、rollback 与静态契约
  全集最终为 `76 passed in 33.19s`。相关 Ruff `All checks passed!`、两份
  Shell `bash -n` 与 `git diff --check` 均退出 0；文档已同步四类状态与旧
  release 复验时点。本阶段未 build/package、联网、访问服务器、commit 或
  push。

## 2026-07-30 首次部署入口收口：最终验收与提交授权

- [x] 唯一有效的最终全量 pytest 退出 0：
  `584 passed, 61 warnings in 556.36s`，skipped=0；高于本轮任务 0 的
  562 passed。warning 类别仍只有既有 `StarletteDeprecationWarning` 与
  `UserWarning`，没有新增类别。
- [x] 最终静态门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 93 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、`git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] 最终临时 Git index release-safety 为 `tracked_files=236`，binary、
  large、local path、private network、private path、secret 与总
  `violations` 均为 0；临时 index 已在确认是 `/tmp` 下普通文件后精确删除。
- [x] 最终只修改 8 个白名单文件：`PROGRESS.md`、两份部署脚本、两份部署
  文档和三份对应测试；新增 skip/xfail/TODO=0，三份冻结 deployment config
  diff=0。提交前真实 index staged=0，`git write-tree` 与
  `HEAD^{tree}` 均为 `3c1f07178bf6dc688138494d0017a8737e9e5289`。
- [x] 保护摘要与任务 0 完全一致：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked=182/聚合分别为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`。
- [x] 专用本地 `rag-final-three-qdrant` 最终仍 running、
  OOMKilled=false、RestartCount=0、mounts=[]、collection=0，且只绑定
  `127.0.0.1:6333`。本轮没有 build/buildx、image save/load、真实
  package、联网安装、SSH/SCP 或 `.57/.58/.60` 操作。
- [x] 用户在实现和验收完成后最新明确要求“将代码 commit 并 push”，因此
  仅覆盖本任务书的 commit/push 禁令；不扩大到任何其他外部操作。
  `BLOCKED.md` 继续保留真实模型消融/revision、Word 自动编号、GPU OCR、
  EMF、完整 chat-template token 预算和正式生产验收。

## 2026-07-30 部署文档身份契约任务 0：事实基线

- [x] 当前 HEAD 为
  `db6e9237e6f28833597f1db5319c4af8cc34ce5f`，`main` 与
  `origin/main` 同步，工作树和暂存区均为空。`git write-tree` 与
  `HEAD^{tree}` 均为 `c6c3da6f2ccd7ee8998e7ac9d72667307e5a431a`。
- [x] 当前 HEAD 全量 pytest 退出 0：
  `584 passed, 61 warnings in 579.95s`，skipped=0；warning 类别仍只有
  `StarletteDeprecationWarning` 与 `UserWarning`。
- [x] 基线静态门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 93 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose 和 `git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] 当前 HEAD 的 release-safety 为 `tracked_files=236`，binary、large、
  local path、private network、private path、secret 与总 `violations`
  均为 0。
- [x] 保护摘要继续为 docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked=182/聚合分别为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`。
  专用本地 `rag-final-three-qdrant` 为 running、OOMKilled=false、
  RestartCount=0、mounts=[]，且只绑定 `127.0.0.1:6333`。
- [x] 本阶段未修改源码、部署实现、Compose 或三份冻结配置；未执行 build、
  save/load、package、联网、`.57/.58/.60`、服务器操作、commit 或 push。

## 2026-07-30 部署文档身份契约任务 1：release 身份术语

- [x] 两项文档契约测试先在旧文档上真实红灯：
  `2 failed, 4 deselected in 0.04s`。其中 release 身份用例证明短 README
  缺少推荐的 `revision/release_id` 命令、把 `release_id` 错写为 40 位
  Git SHA，且两份文档都没有从 runtime `RELEASE_ID/SOURCE_REVISION`
  重新读取服务器身份。
- [x] 两份文档现统一定义：`revision` 是完整 40 位小写 Git SHA；
  `release_id` 是 runtime `RELEASE_ID`，未显式覆盖打包变量时默认为
  `revision` 前 12 位。两份文档均给出
  `revision="$(git rev-parse HEAD)"` 与
  `release_id="${revision:0:12}"`。
- [x] 服务器候选流程从已校验 runtime 的 `RELEASE_ID` 和
  `SOURCE_REVISION` 读取变量；release 目录、镜像 tag、归档名和
  `candidates/${release_id}.env` 使用 `release_id`，候选文件中的
  `RAG_RELEASE_REVISION` 使用完整 `revision`。长手册还用独立
  `expected_release_id` 完成解包前归档定位和解包后身份复核。
- [x] 静态测试同时绑定 `package.sh` 写入 `RELEASE_ID/SOURCE_REVISION`
  和 `deploy.sh` 读取两文件、按 release ID 校验 candidate 路径的现有契约；
  禁止重新出现 `release_id='<40位小写Git SHA>'`。定向绿测为
  `1 passed, 5 deselected in 0.02s`；相关 Ruff
  `All checks passed!` 与 `git diff --check` 均退出 0。
- [x] 本阶段仅修改两份允许文档、对应静态测试和本进度记录；未修改
  package/deploy 等部署实现，也未执行 build/package/联网/服务器操作、
  commit 或 push。

## 2026-07-30 部署文档身份契约任务 2：Docker 镜像身份

- [x] 与任务 1 同轮建立的镜像身份静态测试在旧长手册上真实红灯；失败点为
  缺少同时输出平台、`.Id` 和 `.RepoDigests` 的只读命令，且旧文档仍要求
  “镜像 ID 必须分别等于引用中的 digest”。两项初始红测合计为
  `2 failed, 4 deselected in 0.04s`。
- [x] 长手册现在分别定义 `.Id` 为当前 Docker daemon 的本地 image ID，
  `.RepoDigests` 为 registry 来源核验依据；明确两者属于不同身份域，不得
  比较 `.Id == RepoDigest`。
- [x] 只读示例使用
  `docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}
  {{range .RepoDigests}}{{println .}}{{end}}'`。Python、OCR、Qdrant 三个固定
  引用分别要求 `linux/amd64`，并以 `grep -Fx` 证明 RepoDigests 精确包含各自
  批准的 canonical RepoDigest；没有把本地 image ID 当作 registry digest。
- [x] 新反测同时扫描两份部署文档，禁止再次出现 image ID 必须等于 digest
  或 `.Id` 与 RepoDigest 相等的正向要求。镜像身份定向绿测为
  `1 passed, 5 deselected in 0.03s`；完整文档事务静态测试为
  `6 passed in 0.02s`，相关 Ruff `All checks passed!` 和
  `git diff --check` 均退出 0。
- [x] 本阶段仍只修改允许的长手册、静态测试和进度记录；没有执行
  `docker pull`、build、save/load、package、联网、服务器访问、commit 或
  push。

## 2026-07-30 部署文档身份契约：最终验收

- [x] 最终全量 pytest 退出 0：
  `586 passed, 61 warnings in 575.95s`，skipped=0；高于任务 0 的
  584 passed，warning 类别仍只有既有 `StarletteDeprecationWarning` 与
  `UserWarning`，没有新增类别。
- [x] 最终静态门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 93 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、`git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] 最终只有 4 个允许文件有差异：`PROGRESS.md`、
  `deployment/README.md`、长版离线部署手册和对应静态测试；
  unexpected=0、missing=0。`src/`、三份 deployment config、Compose 和
  deploy/rollback/install/package/backup 实现均为零 diff；新增
  skip/xfail/TODO=0。
- [x] 保护摘要与任务 0 完全一致：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`。
- [x] 只读参考仓库仍 clean，HEAD/tree/tracked=182/聚合分别为
  `03d51db2c0e57ade04c8f9fe035316907d2717f5` /
  `84a0a960426da37111a93a806242543c61a881a9` /
  `44254dffe64a2a1a18ab9b5fdb86025650a99c6d136fca5c48161b0d7879297a`。
- [x] 最终临时 Git index release-safety 为 `tracked_files=236`，binary、
  large、local path、private network、private path、secret 和总
  `violations` 均为 0；确认临时 index 是 `/tmp` 下普通文件后已精确删除。
  真实 index staged=0，`git write-tree` 与 `HEAD^{tree}` 均为
  `c6c3da6f2ccd7ee8998e7ac9d72667307e5a431a`。
- [x] 测试后专用 `rag-final-three-qdrant` 为 running、
  OOMKilled=false、RestartCount=0、mounts=[]、collections=0，且只绑定
  `127.0.0.1:6333`。额外只读 inspect 证明本地已有 Python/Qdrant 镜像的
  RepoDigests 包含手册 canonical digest；OCR 固定镜像本地未加载，按边界
  没有 pull 或联网补齐，也未把缺失的本地实测冒充为文档契约证据。
- [x] `BLOCKED.md` 继续保留真实模型消融、Word 自动编号、GPU OCR、EMF、
  完整 chat-template token 预算和生产验收。当前 HEAD 仍为
  `db6e9237e6f28833597f1db5319c4af8cc34ce5f`；本轮未 build、save/load、
  package、联网、访问 `.57/.58/.60` 或服务器，也未 commit/push。

## 2026-07-30 部署文档身份契约：后续提交授权

- [x] 用户在完整验收后明确要求“把代码 commit 并 push”，仅覆盖本任务的
  commit/push 禁令；不扩大到 build、package、服务器访问或其他外部操作。
- [x] 提交范围继续限定为两份部署文档、对应静态测试和本进度记录。

## 2026-07-30 Query rewrite 与真实模型契约任务 0：当前 HEAD 基线

- [x] 中断后已先读本文件与 `BLOCKED.md`。当前 HEAD 为
  `1eb05986058f791d7f9b8705911a5219b86be7d9`，`main` 与
  `origin/main` 同步，工作树和暂存区为空；`git write-tree` 与
  `HEAD^{tree}` 均为 `43fc81b4006a61de675167865e019d16efe762f7`。
  该 HEAD 已包含用户在上一阶段明确授权提交的文档身份修复；从本阶段恢复后
  未再 commit/push。
- [x] 当前 HEAD 全量 pytest 基线实际为
  `1 failed, 585 passed, 61 warnings in 567.76s`，skipped=0，达到不少于
  584 passed 的基线要求。唯一失败是白名单外
  `test_target_verifier_rejects_corrupt_sqlite_state` 在覆写 SQLite 主文件后
  未抛错；未修改源码或测试，随后原样定向复核为
  `1 passed, 1 warning in 5.57s`。因此保留首次全量非绿事实，不把定向通过
  冒充全量通过；warning 类别仍只有既有 `StarletteDeprecationWarning`
  与 `UserWarning`。
- [x] 其余基线门禁退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 93 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、11/11 `deployment/ASSETS.sha256` 和 `git diff --check`
  均通过。首次 mypy 命令误把配置排除的 `tests/` 显式传入而退出 2，改用
  仓库既有 `src scripts evaluation` 全量口径后通过，不作为产品失败。
- [x] 当前 HEAD release-safety 为 `tracked_files=236`，binary、large、
  local path、private network、private path、secret 与总 `violations`
  均为 0。
- [x] 保护摘要为 docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`。
  retrieval 仍为 `provisional`，embedding/reranker/LLM revision 仍为
  `pending-server-verification`，未伪造 frozen 或 revision。
- [x] 本阶段未修改源码、测试、部署实现或冻结配置；未执行 build、package、
  联网安装、访问 `.57/.58/.60` 或其他服务器操作。

## 2026-07-30 Query rewrite 任务 2：确定性触发规则

- [x] 新测试先在旧实现上真实红灯：
  `5 failed, 7 deselected in 0.97s`。其中“其他情况怎么处理”证明裸字符
  `"其"` 会误调用 LLM；时间、序号、继续请求三类证明旧规则漏触发；既有
  “上述”用例证明 Trace 缺少稳定的 trigger 分类码。
- [x] 删除裸 `"其"` 子串规则，保留明确短语“其中”；新增四类确定性规则：
  这个/这些/那个/那些/它/其中/上述/前述/前者/后者归为
  `REWRITE_TRIGGER_PRONOUN`，刚才/前面/上面/上一条或项或个归为
  `REWRITE_TRIGGER_TEMPORAL`，`第N种/项/条/个` 归为
  `REWRITE_TRIGGER_ORDINAL`，有界的继续/再详细/还有吗/然后呢/那怎么办
  归为 `REWRITE_TRIGGER_CONTINUATION`。
- [x] “其他、其次、尤其”、仅含“该”或“其”的独立问句以及句末“呢”均不
  触发。Trace 新增的 `trigger_reason_code` 只保存稳定类别码，不保存命中词
  或匹配正文；无命中时为 null。改写 revision 已纳入四类规则与正则。
- [x] 红测修复后为 `5 passed, 7 deselected in 0.89s`；整份 rewrite 测试为
  `12 passed in 0.65s`。相关 Ruff `All checks passed!`、mypy
  `Success: no issues found in 2 source files` 与 `git diff --check`
  均退出 0。
- [x] 完成审计进一步发现 QueryService 的 SAFE span 只持久化顶层
  `reason_code`，原实现成功时仍写通用 `REWRITE_OK`，无法留下具体触发类别。
  先改测试后定向真实红灯为 `7 failed, 13 deselected in 0.87s`；修复后顶层
  `reason_code` 保存具体 trigger 类别，独立 `rewrite_result_code` 保存
  `REWRITE_OK`，定向复核为 `7 passed, 13 deselected in 0.90s`。FULL Trace
  仍保留既有诊断正文，SAFE span 只记录类别和计数，不记录匹配词或正文。
- [x] 完成审计后的 rewrite、query service、hybrid 和模型契约回归合计
  `33 passed, 1 warning in 4.98s`；既有 Qdrant warning 类别未变化。
- [x] 本阶段只修改 rewrite、稳定原因码、对应测试和本进度记录；未改
  chunking、检索融合、rerank、回答发布协议或冻结配置，也未执行
  build/package、联网、服务器访问、commit/push。

## 2026-07-30 Query rewrite 任务 3：anchor 漂移守卫

- [x] 新 anchor 测试先在旧实现上真实红灯：
  `6 failed, 2 passed, 12 deselected in 0.93s`。3号→2号、5%→10%、
  2026-07-30→2026-07-31、v2→v3、GB/T19001-2016→2015 和
  “Alpha方案”→“Beta方案”六种漂移均被旧实现错误接受；从选中历史补入
  A-17 或“Alpha方案”的两个合法用例保持通过。
- [x] 新守卫从当前问题、选中历史和候选改写中确定性提取并规范化日期、
  百分比、普通数字、序号、点分条款号、字母数字设备/版本/标准号，以及
  中文或英文引号中的名称。当前问题的全部 anchor 必须仍存在，改写新增
  anchor 必须属于 token 预算内的选中历史；校验不读取历史答案。
- [x] schema 合法但 anchor 缺失、修改或凭空增加时，查询变体只保留原问题，
  `resolved_query` 回退原问题并记录稳定
  `REWRITE_ANCHOR_DRIFT`。Trace 不写 anchor 值，只沿用摘要、计数和稳定
  原因码；anchor 正则已纳入 rewrite revision。
- [x] 修复后 anchor 定向为 `8 passed, 12 deselected in 0.67s`，整份
  rewrite 为 `20 passed in 0.66s`。rewrite、query service 和真实 Qdrant
  hybrid 回归合计 `22 passed, 1 warning in 5.15s`，继续证明原问题与合法
  改写同时召回、resolved_query 用于检索/重排、回答仍使用原问题。
  相关 Ruff、2 文件 strict mypy、changed Google docstring 与
  `git diff --check` 均退出 0。
- [x] `rewrite.py` 从基线 496 行增至 661 行，超过“手写模块宜 ≤400 行”的
  建议值；本任务硬白名单不允许新增 anchor 辅助模块，因此保留在唯一允许的
  rewrite 模块内，并拆成 5 个窄职责私有函数，没有借机修改其他模块。
- [x] 本阶段仍未改 chunking、索引事务、Dense/BM25、RRF、rerank 参数、
  回答发布协议或冻结配置；未 build/package、联网、访问服务器、commit/push。

## 2026-07-30 真实模型契约任务 4：用户执行脚本

- [x] 新测试先在当前仓库真实红灯：pytest 收集期因
  `ModuleNotFoundError: scripts.verify_model_contracts` 退出 1，证明原仓库
  完全缺少该交付物。新增脚本后首次功能轮为 `10 passed, 1 failed`；唯一
  失败是 HTTPX 在进入产品代码前拒绝 Mock 的 `Infinity` 序列化，改用明确
  原始 JSON 夹具后产品的非有限向量拒绝码得到真实覆盖。
- [x] 新只读 CLI 对 embedding、reranker、LLM 分角色接受 endpoint、model、
  token 环境变量名、超时和 embedding dimension；只调用 `/health`、
  `/v1/models` 及该角色的一次最小业务端点，不导入 DOCX/Qdrant 代码，
  不写文件或数据库，并以 `trust_env=False` 禁止误走环境代理。
- [x] 三类服务均要求 health=200、models 中 model ID 唯一匹配且 endpoint
  revision 可从受限字段/响应头提取。embedding 严格校验两条响应的数量、
  连续 index、配置维度和全部有限数值；reranker 严格校验两条响应的数量、
  连续 index 和 `[0,1]` 有限分数。
- [x] LLM 分别发送与生产结构一致的 query rewrite 和 strict evidence answer
  JSON Schema；两次请求都固定 temperature=0、stream=false、
  `enable_thinking=false`，只接受单 choice、`finish_reason=stop`、匹配 model、
  合法 schema 和自洽 prompt/completion/total token 计数。
- [x] 报告只含 service、净化 endpoint、model、revision、通过项、维度/条数/
  index、分数范围、finish reason 和 token 计数；不输出 token、问题、prompt
  或完整响应。失败只输出稳定错误码。MockTransport 最终为
  `11 passed in 0.05s`，覆盖三角色成功、错 model、错 schema、错维度、
  非有限向量、reranker 条数/index/分数、截断和 endpoint failure。
- [x] 脚本 `--help` 退出 0；缺失 token 环境变量的真实 CLI 调用输出仅含
  `TOKEN_ENV_MISSING` 等脱敏元数据并按预期退出 1。相关 Ruff
  `All checks passed!`、strict mypy
  `Success: no issues found in 1 source file`、changed Google docstring
  `missing_google_sections=0` 与 `git diff --check` 均退出 0。
- [x] `verify_model_contracts.py` 为 607 行，超过“手写模块宜 ≤400 行”的建议；
  硬白名单只允许新增这一份脚本，且两个严格 schema 必须随离线脚本自包含，
  因此未新增白名单外模块。执行、三角色 probe、响应校验和 CLI 仍拆成窄职责
  函数，单项测试文件为 298 行。
- [x] `BLOCKED.md` 已加入三个角色的用户执行命令和四个 LLM 分别执行要求。
  本轮没有访问真实端点，retrieval 保持 `provisional`，模型 revision 保持
  `pending-server-verification`；未 build/package、联网、访问服务器、
  commit/push。

## 2026-07-30 Query rewrite 与真实模型契约：最终验收

- [x] 全量验收严格止于 3 轮。第 1 轮为
  `1 failed, 609 passed, 61 warnings in 576.56s`，唯一失败是白名单外
  target verifier 读取 Qdrant collection 超时，原样定向复核为
  `1 passed, 1 warning in 4.87s`。第 2 轮为
  `2 failed, 608 passed, 61 warnings in 567.79s`：一项在清理 Qdrant
  collection 时超时，另一项是既有 SQLite/WAL 覆写用例偶发未抛错；原样
  定向复核中 Qdrant 项通过，SQLite 项先失败，未改代码或测试的下一次独立
  运行通过。没有把定向通过冒充全量通过。
- [x] 第 3 轮、也是最后一轮全量 pytest 退出 0：
  `610 passed, 61 warnings in 563.83s`，skipped=0，高于 584 passed 基线；
  warning 类别仍只有既有 `StarletteDeprecationWarning` 与 `UserWarning`。
- [x] 最终静态门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 94 source files`；changed Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、`git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] 最终候选树严格只有 7 个允许文件：`BLOCKED.md`、`PROGRESS.md`、
  `scripts/verify_model_contracts.py`、`src/rag_app/retrieval/rewrite.py`、
  `src/rag_app/tracing/reasons.py`、`tests/test_query_rewrite.py` 和
  `tests/test_verify_model_contracts.py`；unexpected=0、missing=0。
  新增 skip/xfail/TODO=0。
- [x] 最终临时 Git index release-safety 为 `tracked_files=238`，binary、
  large、local path、private network、private path、secret 和总
  `violations` 均为 0；临时 index 仅用于审计并已精确删除。真实 index
  staged=0，`git write-tree` 与 `HEAD^{tree}` 均为
  `43fc81b4006a61de675167865e019d16efe762f7`。
- [x] 保护摘要与任务 0 完全一致：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别仍为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`。
- [x] retrieval 仍为 `provisional`，模型 revision 仍为
  `pending-server-verification`。真实模型命令和解除条件已写入
  `BLOCKED.md`；本轮没有 build/save/load/package、联网、访问
  `.57/.58/.60` 或其他服务器，也未 commit/push。

## 2026-07-31 生产前四项代码阻塞任务 0：当前 HEAD 基线

- [x] 中断后已先读本文件与 `BLOCKED.md`。当前 HEAD 为
  `2ef35fbb5f81a4700fd2330b0e124165b5f8eed7`，`main` 与
  `origin/main` 同步，工作树和暂存区均为空；`git write-tree` 与
  `HEAD^{tree}` 均为 `dd6cf786cebced4326c613692abe9d00e5a06659`。
- [x] 当前 HEAD 全量 pytest 退出 0：
  `610 passed, 61 warnings in 539.22s`，skipped=0；warning 类别仍只有
  既有 `StarletteDeprecationWarning` 与 `UserWarning`。
- [x] 基线静态门禁全部退出 0：`compileall -q src tests scripts` 无输出；
  Ruff `All checks passed!`；mypy
  `Success: no issues found in 94 source files`；Google docstring
  `missing_google_sections=0`；全部 deployment Shell、默认/index profile
  Compose、`git diff --check` 均通过；`deployment/ASSETS.sha256` 11/11
  全部 `OK`。
- [x] 当前 HEAD release-safety 为 `tracked_files=238`，binary、large、
  local path、private network、private path、secret 与总 `violations`
  均为 0。
- [x] 保护摘要仍为 docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别为
  `f61a74b0dc2ad8d9e35261b6ea3717848ea6dfc3d78e427ca1b3dbc8a8538d8c` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`；
  retrieval 继续为 `provisional`，模型 revision 仍为
  `pending-server-verification`。
- [x] 基线索引指纹为
  `sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a`，
  serving 指纹为
  `sha256:12efce30ad97d9d74ddd2c437110dd883ee87ba02ae9aa998b7fa6e34f94a79b`。
- [x] 独立直接加载 checked-in pipeline 后真实复现本轮首个阻塞：
  配置 `prompt_revision` 为
  `sha256:9fc5318f48fe38a5941cf6b8738c9725dcc3aebaa5f55bc4b698ecf55e4398d7`，
  当前实现实际计算为
  `sha256:098de50b828075a19e6ca1a1bb4ff8708f143b4b9a766021703cb6f7c9cbb250`，
  `matches=False`。
- [x] 本阶段除本进度记录外未修改源码、测试、部署文件或冻结配置；未执行
  build/package、联网、访问 `.57/.58/.60`、commit 或 push。

## 2026-07-31 生产前四项代码阻塞任务 2：模型契约验证器

- [x] 新增反测先真实退出 1：选定三项为
  `3 failed, 11 deselected in 0.09s`，分别证明原实现强制 token、没有拒绝
  endpoint revision 漂移、LLM 只发两次最小请求且没有最大初次/repair
  上下文预算证据。
- [x] 验证器现要求明确 `expected_revision`，拒绝
  `unknown/main/latest`；无 token 时不发送 Authorization，有非空 token 时
  才发送 Bearer。endpoint 返回的 model/health/header revision 必须全部合法、
  一致并精确匹配；缺失时只接受非符号链接、无写权限且规范化 SHA256 正确的
  deployment manifest，精确绑定 endpoint、model、model/tokenizer/code
  revision、vLLM、quantization、max context 和 chat-template SHA。
- [x] rewrite、最大初次回答与最大 repair 三个请求均直接由
  `rag_app.model_contracts` 构造，固定 production schema、temperature=0、
  stream=false、thinking=false 和请求字段；报告只保存脱敏 usage，并以服务
  返回的 prompt tokens 校验
  `prompt_tokens + max_output_tokens <= context_limit`。
- [x] 当前 MockTransport 契约套件为 `22 passed in 0.30s`；Ruff 为
  `All checks passed!`，脚本 strict mypy 为
  `Success: no issues found in 1 source file`。没有读取 DOCX、写 Qdrant、
  输出 token/完整响应或访问真实模型端点；真实执行项和新命令继续保留在
  `BLOCKED.md`。

## 2026-07-31 生产前四项代码阻塞任务 1：唯一模型契约与 pipeline

- [x] checked-in pipeline 直接断言先真实红灯：
  `1 failed in 1.02s`；配置 revision 为
  `sha256:9fc5318f48fe38a5941cf6b8738c9725dcc3aebaa5f55bc4b698ecf55e4398d7`，
  当时实际契约为另一 SHA，证明 app 前置校验阻塞可复现。
- [x] 新增唯一 `rag_app.model_contracts`，集中保存 rewrite/answer system
  prompt、严格 JSON Schema、生产请求/repair 构造、结构解析、固定生成字段和
  revision。QueryRewriter、AnswerGenerator、runtime 与模型验证器均直接引用，
  对 src/scripts 搜索确认 prompt/schema 名称和正文只在该模块出现。
- [x] 全部代码变更结束后才把 pipeline prompt revision 更新为
  `sha256:2319cc44f026c6e507b68da62db700311abe55d2c3c019f462105e6b5ded4631`
  并同步 ASSETS。直接契约测试现为 `1 passed in 0.77s`，
  `checked == actual` 为 true，资产为 11/11 `OK`。
- [x] pipeline 变更前后 index fingerprint 均为
  `sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a`；
  serving fingerprint 从
  `sha256:12efce30ad97d9d74ddd2c437110dd883ee87ba02ae9aa998b7fa6e34f94a79b`
  变为
  `sha256:9a56a82dc8458b01d1bf4f3e26cd596b35a46d52bad15c3692ed9581310725ca`。
  chunking、索引事务和 retrieval 配置均未修改。

## 2026-07-31 生产前四项代码阻塞任务 3：多轮改写语义

- [x] 新反测分别真实得到 `4 failed, 1 passed, 20 deselected in 0.73s`
  与 `1 failed in 0.60s`：裸“设备上面”误报 temporal，rewriter 不接受
  verified claims，ConversationStore 也没有 `append_turn`。
- [x] 裸“前面/上面”不再触发；“前面提到/上面说到/上文所述”等话语回指
  仍触发。第 N 条/章/款继续作为事实 anchor，数字、日期、百分比、设备号、
  版本、标准号和引用名称的漂移门禁保持不变；第 N 种/项/个、前者/后者只按
  上一轮已验证 claim 的稳定顺序确定性解析，越界时零模型调用、回退原问题并
  在 trace 记录 `REWRITE_CONTEXT_UNRESOLVED`。
- [x] SQLite 在同一事务中只投影 AnswerResult 的有序 claim text、chunk ID
  与 locator，不保存 quote、evidence ID、answer trace 或 raw model output；
  读取只返回受 TTL/轮数限制的问题和最后一轮 claims，拒答轮不会沿用更早
  claims。QueryService 已同时传递这两类有限上下文并在发布时调用
  `append_turn`。
- [x] rewrite/store 套件为 `27 passed in 0.82s`；QueryService/API/Trace
  定向套件为 `19 passed, 1 warning in 4.26s`；相关 Ruff 与 strict mypy
  均退出 0。

## 2026-07-31 生产前四项代码阻塞任务 4：显式 shared corpus

- [x] 两项新反测先真实为 `2 failed in 0.70s`：缺失 access mode 错误地能
  构造设置，`shared_corpus` 又被当作未知字段。
- [x] `RuntimeSettings.access_mode` 现为必填单值 `AccessMode`，只接受
  `shared_corpus`；缺失和 `permissioned` 都在 Pydantic 启动配置解析阶段
  失败。settings/runtime construction 定向套件为 `13 passed in 2.24s`，
  Ruff 与 settings strict mypy 均退出 0。
- [x] `deployment/.env.example`、部署 README 和公开离线手册已明确：所有
  query-token 用户可检索全部 `active`/`official` 文档，V1 没有用户、租户或
  文档级权限，不能把 `permissioned` 伪装成已实现。
- [ ] `deployment/compose.yaml` 显式枚举环境变量却未映射新增值；当前硬
  白名单不允许修改该文件，因此真实容器会因缺少必填配置失败。最小解除改动
  和复验条件已置顶写入 `BLOCKED.md`，其余不受影响项继续。

## 2026-07-31 生产前四项代码阻塞：完整验收

- [x] 第 1 轮全量 pytest 真实退出 1：
  `1 failed, 632 passed, 61 warnings in 601.05s`，skipped=0。唯一失败是
  `test_provisional_configuration_files_remain_unchanged` 仍冻结旧
  pipeline 文件 SHA；功能测试全部通过。
- [x] 只把该精确 SHA 锁从旧值同步为本任务明确授权的新 pipeline 文件
  `87734d37e2fab9d08585b84adf65a61751af1021b74f888195cc3c5f37d54bbf`，
  未删除/放宽断言，也未修改 retrieval SHA 或任何阈值。原失败用例定向复核为
  `1 passed in 0.03s`。
- [x] 第 2 轮全量 pytest 退出 0：
  `633 passed, 61 warnings in 588.55s`，skipped=0；warning 数量与类别均
  未超过基线；后续 release-safety 触发局部源码重命名，因而仍需第 3 轮。
- [x] 候选 release-safety 首次真实退出 1：
  `tracked_files=239, secret_matches=1, violations=1`，定位为验证脚本局部
  凭据变量的跨行赋值形态被扫描器保守命中；未忽略规则，而是改用
  `authorization_value` 命名。验证器套件仍为 `22 passed in 0.30s`，
  候选扫描随后为 `tracked_files=239, violations=0`。
- [x] 因第 2 轮后存在上述局部源码重命名，使用允许的第 3 轮完整验收；最终
  pytest 退出 0：`633 passed, 61 warnings in 557.38s`，skipped=0。三轮
  上限已用完，最终轮没有失败或退化。
- [x] 最终源码静态门禁均退出 0：compileall 无输出；Ruff
  `All checks passed!`；strict mypy
  `Success: no issues found in 95 source files`；changed Google docstring
  `missing_google_sections=0`；6 个 deployment Shell、默认/index Compose、
  `git diff --check` 和 ASSETS 11/11 均通过。
- [x] 保护摘要与任务 0 完全一致：docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  retrieval/corpus SHA 仍为基线；真实 index staged=0，`git write-tree` 与
  `HEAD^{tree}` 均为 `dd6cf786cebced4326c613692abe9d00e5a06659`。
- [x] 本目标没有 build/save/load/package、联网、访问 `.57/.58/.60`、
  commit 或 push；retrieval 仍为 `provisional`，模型 revision 仍为
  `pending-server-verification`。唯一未闭环项是硬白名单外的 Compose
  access-mode 映射，已置顶保留在 `BLOCKED.md`。

## 2026-07-31 生产前四项代码阻塞：自动续跑第 1 次完成审计

- [x] 先重新读取 `PROGRESS.md`、`BLOCKED.md` 和当前 status；没有 reset、
  checkout、build、package、联网、服务器、commit 或 push，也没有重跑已经
  完成的全量验收。
- [x] 当前源码复核仍证明任务 1—3 完成：rewrite/answer prompt 与 schema
  在 src/scripts 中均只有 `model_contracts.py` 一份；checked/actual prompt
  revision 一致；index fingerprint 仍为
  `sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a`，
  serving fingerprint 仍为
  `sha256:9a56a82dc8458b01d1bf4f3e26cd596b35a46d52bad15c3692ed9581310725ca`，
  retrieval 仍为 `provisional`。
- [x] 任务 4 的设置、反测、样例和权限文档仍满足源码层要求；但实际 Compose
  JSON 中 app/worker 的 `RAG_ACCESS_MODE` 均为缺失。Dockerfile 不复制
  `.env.example`，Compose 也没有 `env_file`，因此白名单内没有可保持该字段
  显式必填的替代方案。
- [ ] 同一 Compose 白名单阻塞已连续出现在首次交付和本次自动续跑，共 2 个
  goal turn；尚未达到 3 次阻塞审计阈值，目标保持 active。解除条件不变：
  授权修改 `deployment/compose.yaml` 两处环境映射并同步对应资产/测试。
- [x] 文档记录后的轻量门禁通过：候选临时索引 release-safety
  `tracked_files=239`、`violations=0`，候选与工作树 `diff --check` 均退出 0；
  真实索引 staged=0，`HEAD^{tree}` 与 `git write-tree` 均为
  `dd6cf786cebced4326c613692abe9d00e5a06659`，临时索引已删除。首次包装命令
  因 PowerShell/WSL 变量传递在复制步骤退出 1，未进入扫描且未修改仓库；
  随后改用固定临时路径完成上述绿色证据。

## 2026-07-31 生产前四项代码阻塞：自动续跑第 2 次阻塞审计

- [x] 先重新读取 `PROGRESS.md`、`BLOCKED.md` 与当前 status；HEAD 为
  `2ef35fbb5f81a4700fd2330b0e124165b5f8eed7`，真实 staged=0，
  `git write-tree` 与 `HEAD^{tree}` 均为
  `dd6cf786cebced4326c613692abe9d00e5a06659`。
- [x] Docker Compose v5.1.2 实际解析默认和 `index` profile 均退出 0。
  默认配置的 app/worker 环境键数为 30/1，`index` profile 为 30/31；
  四处 `RAG_ACCESS_MODE` 均为缺失。Compose 相对 HEAD 无 diff，SHA256 为
  `d7849a77e71c554614d6ddd8cd957da8a91ad7230e5fbaa57f5a673296ed3b5c`。
- [x] 排除 `.venv` 后仓库唯一 `.env*` 文件仍是
  `deployment/.env.example`；Dockerfile 不复制它，Compose 也没有
  `env_file`。任务书未授权修改 `deployment/compose.yaml`，不存在可同时
  保持“显式必填”与白名单边界的替代实现。
- [x] 最终候选树 release-safety 为 `tracked_files=239`、
  `violations=0`；候选与工作树 `diff --check` 均退出 0，真实 staged=0，
  `git write-tree` 与 `HEAD^{tree}` 仍同为
  `dd6cf786cebced4326c613692abe9d00e5a06659`，临时索引已删除。
- [x] 同一阻塞现已连续出现 3 个 goal turn；除取得
  `deployment/compose.yaml` 修改授权外已无可继续项，应按目标规则标记为
  blocked。没有 build/package、联网、服务器、commit 或 push，也未重跑已
  用满三轮的完整验收。

## 2026-07-31 RAG_ACCESS_MODE Compose 注入任务 0：当前 HEAD 基线

- [x] 已先读取本文件与 `BLOCKED.md`。当前 HEAD 为
  `26b7d5c7eac412cb0427acd289c01bf91271256c`，`main` 与
  `origin/main` 同步，起始工作树和暂存区均为空；`git write-tree` 与
  `HEAD^{tree}` 均为 `dc910e9e7cb83d19ae62499289bb8dd3ce9a95cf`。
- [x] 保护摘要为 docs
  `36c67e3b7ac38a734b4f5eba00216cd806996bbc23b6d99b856f9763b44e8e0e`；
  artifacts
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`；
  frozen
  `63adcd455c16678f29a5b2d3c6cdf3edc7ccbea4bd3dff8e0c8ba68c4cab5046`；
  results
  `cdb17f0c251a46e523175c632e260804390b63b4ef1d8c68f4c4bc1253df73de`；
  evidence
  `05b845b97ced765a6e48a3be8bc99acbc0913cd38fc891d6513d65a93e3bf3bc`。
  pipeline/retrieval/corpus policy 分别为
  `87734d37e2fab9d08585b84adf65a61751af1021b74f888195cc3c5f37d54bbf` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`；
  `deployment/ASSETS.sha256` 为
  `91a97f380ca212b27ef8909973fd61a157a5b87774f970360ab7258927e26d8f`。
- [x] 当前 HEAD 全量 pytest 基线为
  `633 passed, 61 warnings in 558.55s`，skipped=0、stderr=0；warning
  类别仍只有 `StarletteDeprecationWarning` 与 `UserWarning`。首次用 1 秒
  前台超时探测时进程被工具终止并退出 124，未形成 pytest 结果；随后改用
  隐藏后台 WSL 进程完整执行得到上述有效基线。
- [x] 基线门禁均退出 0：compileall 无输出；Ruff
  `All checks passed!`；strict mypy
  `Success: no issues found in 95 source files`；Google docstring
  `missing_google_sections=0`；6 个 deployment Shell、默认/index Compose、
  `git diff --check` 和 ASSETS 11/11 均通过。release-safety 为
  `tracked_files=239`、`violations=0`。
- [x] Docker Compose v5.1.2 实际解析证明旧实现的默认与 `index` profile
  中，`rag-app` 和 `rag-worker` 四处均不存在 `RAG_ACCESS_MODE`；加载
  `deployment/.env.example` 也不会自动注入未显式映射的变量。Compose
  SHA256 为
  `d7849a77e71c554614d6ddd8cd957da8a91ad7230e5fbaa57f5a673296ed3b5c`。
- [x] 基线阶段没有 build/package、联网、访问 `.57/.58/.60` 或其他服务器，
  也没有 commit 或 push。

## 2026-07-31 RAG_ACCESS_MODE Compose 注入任务 1：红测

- [x] 在既有 `tests/test_worker_deployment_policy.py` 新增真实 Compose
  契约测试，不断言环境键数量。两个用例均先确认
  `deployment/.env.example` 已有 `RAG_ACCESS_MODE=shared_corpus`，再分别
  检查默认 `rag-app` 与 `index` profile `rag-worker` 的解析环境。
- [x] 修复前定向 pytest 按预期退出 1：
  `2 failed, 3 passed in 0.22s`。两项失败均为解析环境中
  `environment.get("RAG_ACCESS_MODE")` 实际为 `None`，证明样例变量不会在
  Compose 未显式映射时自动进入容器。

## 2026-07-31 RAG_ACCESS_MODE Compose 注入任务 2—3：修复与反测

- [x] 实现只改 `deployment/compose.yaml` 两行：在 `rag-app` 与
  `rag-worker` 的 `environment` 中分别加入精确表达式
  `RAG_ACCESS_MODE: ${RAG_ACCESS_MODE:?required}`。没有增加默认值、
  `env_file` 或镜像 COPY，也没有向其他服务注入变量。修复后 Compose
  SHA256 为
  `3d63ef7284698aacba8ecf66c9df2815f469f8d99c7c1d9ec4d8aec3a143539f`。
- [x] 首轮修复绿测为 `5 passed in 0.39s`；补齐反向契约后
  `tests/test_worker_deployment_policy.py` 为
  `7 passed in 0.86s`，专项 Ruff `All checks passed!`。
- [x] 真实 Compose 解析退出 0：默认配置中 `rag-app` 为
  `RAG_ACCESS_MODE=shared_corpus`，且唯一接收者为 `rag-app`；`index`
  profile 中 app/worker 均为 `shared_corpus`，接收者集合精确为
  `{rag-app, rag-worker}`，未注入 `rag-ocr` 或 `rag-qdrant`。
- [x] 新反测从临时 env 删除 `RAG_ACCESS_MODE`，并从 Compose 子进程环境
  清除同名变量；默认与 `index` profile 的 `config --quiet` 均要求非零且
  stderr 包含该变量名。连同既有 RuntimeSettings 缺失值及
  `permissioned` 拒绝测试，合并专项为 `9 passed in 1.14s`。
- [x] pipeline/retrieval/corpus policy SHA256 仍分别为
  `87734d37e2fab9d08585b84adf65a61751af1021b74f888195cc3c5f37d54bbf` /
  `267e419f41f995aaa61f7750a0753d27be7f90c534e04e8c7e87db07b3db41f3` /
  `0d6553c1ac42207c064145357c3a60fa687f6f0ead3a35bfccace42963a07ab0`；
  `deployment/ASSETS.sha256` 仍为
  `91a97f380ca212b27ef8909973fd61a157a5b87774f970360ab7258927e26d8f`，
  本轮未修改四者。

## 2026-07-31 RAG_ACCESS_MODE Compose 注入：完整验收

- [x] 最终快速门禁全部退出 0：compileall 无输出；Ruff
  `All checks passed!`；strict mypy
  `Success: no issues found in 95 source files`；Google docstring
  `missing_google_sections=0`；6 个 deployment Shell、默认/index Compose
  和 ASSETS 11/11 均通过。
- [ ] 最终全量 pytest 唯一实际结果为
  `1 failed, 636 passed, 61 warnings in 524.23s`，skipped=0；warning 类别
  没有增加。唯一失败是范围外既有
  `test_index_gc_dry_run_preserves_active_rollback_and_unknown`，本轮新增
  Compose 测试全部通过。
- [x] 该既有用例再定向复跑两次仍各为 `1 failed, 1 warning`，总计连续失败
  3 次。Qdrant 日志证明两次 snapshot 在同一秒发起并复用同一文件名，导致
  “额外 snapshot”仍是 manifest 已引用对象；已按规则停止重复，并在
  `BLOCKED.md` 记录解除条件。未修改范围外测试、索引或 GC 实现，也未
  skip、删测或放宽断言。
- [x] 候选临时索引 release-safety 为 `tracked_files=239`、
  `violations=0`；候选与工作树 `diff --check` 均退出 0。真实 staged=0，
  `git write-tree` 与 `HEAD^{tree}` 均为
  `dc910e9e7cb83d19ae62499289bb8dd3ce9a95cf`，临时索引已删除。
- [x] 五项保护摘要与任务 0 完全一致；pipeline/retrieval/corpus policy 和
  `deployment/ASSETS.sha256` 内容均未变化。工作树实际仅修改获授权的
  `deployment/compose.yaml`、对应测试、`PROGRESS.md` 与 `BLOCKED.md`；
  原“RAG_ACCESS_MODE 未映射”阻塞段已经删除，其余外部阻塞继续保留。
- [x] 本轮没有 build/package、联网、访问 `.57/.58/.60` 或其他服务器，
  没有 commit 或 push。
- [ ] Compose 启动阻塞本身已经解除，但本 goal 的全量 pytest 全绿完成条件
  因上述范围外既有 GC 测试失败尚未满足；这是恢复后的第 1 个连续阻塞审计
  turn，目标保持 active。

## 2026-07-31 RAG_ACCESS_MODE Compose 注入：自动续跑第 1 次审计

- [x] 已重新读取 `PROGRESS.md`、`BLOCKED.md` 和当前工作树，没有重复执行已
  连续失败 3 次的 Index GC 用例。
- [x] 默认 Compose 的 app 与 `index` profile 的 app/worker 仍解析为
  `RAG_ACCESS_MODE=shared_corpus`，Qdrant/OCR 仍没有该变量；Compose 修复
  状态未退化。
- [x] `tests/test_index_gc.py` 与 `src/rag_app/index` 相对 HEAD 无 diff。
  只读 Qdrant 日志结合请求耗时再次证明两次 snapshot 发起于同一秒，阻塞
  原因与上一 goal turn 完全相同。
- [x] 候选 release-safety 再次为 `tracked_files=239`、`violations=0`；
  候选与工作树 `diff --check` 均退出 0，真实 staged=0，`git write-tree`
  与 `HEAD^{tree}` 均为
  `dc910e9e7cb83d19ae62499289bb8dd3ce9a95cf`，临时索引已删除。
- [ ] 这是恢复后的第 2 个连续阻塞审计 turn，尚未达到 3 次阈值；目标保持
  active。解除条件仍是授权修改范围外测试夹具，不得改索引/GC 生产实现。

## 2026-07-31 RAG_ACCESS_MODE Compose 注入：自动续跑第 2 次阻塞审计

- [x] 已再次读取 `PROGRESS.md`、`BLOCKED.md` 和当前工作树，没有重跑已连续
  失败 3 次的 Index GC 用例。
- [x] 默认 app、`index` app/worker 仍为 `shared_corpus`，Qdrant/OCR 仍未
  注入；`tests/test_index_gc.py` 与 `src/rag_app/index` 相对 HEAD 无 diff，
  两条 Qdrant snapshot 日志仍证明同秒发起。
- [x] Compose 启动阻塞已经稳定解除，当前白名单内所有任务均已完成；唯一
  未满足项仍是范围外既有 GC 用例导致全量 pytest 非零。
- [x] 最终候选 release-safety 为 `tracked_files=239`、`violations=0`；
  候选与工作树 `diff --check` 均退出 0，真实 staged=0，`git write-tree`
  与 `HEAD^{tree}` 均为
  `dc910e9e7cb83d19ae62499289bb8dd3ce9a95cf`，临时索引已删除。
- [x] 同一阻塞现已连续出现于恢复后的 3 个 goal turn；除授权修改
  `tests/test_index_gc.py` 测试夹具外已无可继续项，应按规则把目标标记为
  blocked。
