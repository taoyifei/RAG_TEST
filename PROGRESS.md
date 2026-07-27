# DOCX RAG 交付进度

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
