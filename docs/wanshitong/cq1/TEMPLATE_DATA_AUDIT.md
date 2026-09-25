# CQ1 模板数据六层审计

审计对象：《研发管理计划》DOCX 模板；场景 AB-017 至 AB-020。记录于 2026-09-25。此文件只含短语、摘要、计数和索引身份；不保存原始业务文档、回答全文、凭据或送模原文。

## 证据身份与边界

- 用户提供的 `AB实测结果_20260925.zip`：SHA-256 `53c08b132b9b739b0a83bc795ce4c5c600556226ef48e54e34e86d1dc82c622e`。其 `AB完整受控证据.tar.gz/corpus-evidence/DOCX-028.docx` 与桌面原件的 SHA-256 均为 `c2da3839f3c6099647f3d5581a0864f4c56c56cb2be4fb9929fffc74418d40e1`，大小 879375 字节。
- 用户服务的实际链路是 54:`18288` → 60:`8288` 前缀代理 → 60 回环 `18290` → `wanshitong-wb08r-root-production-app`。其库和 8289 候选库仅通过容器内 SQLite 的只读 URI `file:/data/universal-rag.sqlite3?mode=ro` 检查；没有改动服务、索引、容器或镜像。这里的“当前”是审计时的活动状态，**不等同于历史 A/B 运行时状态**。
- A/B 封包含 B 侧的 `retrieved_candidates`，以及 A 侧的草稿、状态、Trace ID 和最终引用；没有 A 侧 `retrieved_candidates` 或完整模型输入包。原 A/B 隔离库已经由该次实测清理，四个 A Trace ID 在当前生产和 8289 库中也未找到。故 A 侧历史精确命中集与送模正文是 **missing（不可回溯）**，不能从 B 侧候选或当前库推定。

## 六层逐项结论

| 层 | 历史 A/B 证据 | 18288 当前活动版本 | 8289 当前活动版本 | 本地修复后离线重放 |
| --- | --- | --- | --- | --- |
| 1. 原件 | **present**：DOCX-028 与桌面原件同为 `c2da…d40e1`；关键说明、示例和结构标题在原件。 | **transformed**：当前 `dver_52704274b128fa2dd6a7e9500597cf67` 的源 SHA 为 `2dc54f21…8f7`，不是 A/B 原件；库中另有已 superseded 的原件版本 `dver_555130b1fa1ab3a0c04642b5458d6c5e`，SHA 为 `c2da…d40e1`。 | **transformed**：当前 `dver_d7bbaec69cdecc2a5282d0400876ee85` 的源 SHA 为 `89e7a49b…44a72`，不是 A/B 原件；该库未见 `c2da…d40e1` 版本。 | **present**：输入同一份 `c2da…d40e1` 原件，上传函数保持字节身份。 |
| 2. 解析 IR | A/B 未封存 A 侧 IR；原件重新解析可产生真实段落。 | **transformed**：活动 `revision_documents.document_ir_json` 仅 2 节点（1 个 66 字目录提示段落及 1 个空 section）；目标填写段落 **missing**。已退休的原件修订 `irev_8ce04458f3be7cd988014056b60525bb` 为 197 节点，但未活动。 | **transformed**：活动 IR 为 2 节点，同一目录提示。 | **present**：`docx-ooxml-v4` 产出 197 节点；10 个抽查原件段落的文本摘要在 IR 均精确匹配。 |
| 3. 阅读视图 | A/B 未封存 A 侧阅读视图。 | **excluded**：活动 IR 已无真实说明；当前可供分段的内容只剩目录提示。 | **excluded**：同左。 | **present**：7 个阅读域；10 个抽查段落均完整出现，保留“例如”和占位文本，不将其改写为既成事实。 |
| 4. 活动索引 | A/B A 侧活动索引快照缺失；B 的命中表明旧 WeKnora 候选库当时有模板片段，不能代替 A 侧快照。 | **transformed**：活动修订 `irev_abb7674413b8d095789d6191246310eb`，该文档 1 个 chunk，`citation_text` SHA-256 `9c98f3c6a6afcd3ea0cd41b209ed7315ffa1bca161348bd385c6099182f0edba`，仅目录提示，真实说明 **missing**。 | **transformed**：活动修订 `irev_0329a35ca9ea700133f7b309160118f0`，同为 1 个上述摘要的提示 chunk。 | **present（预览，非活动索引）**：WeKnora chunker 产生 10 个预览 chunk；10 个抽查段落均在 citation text，来源跨度覆盖率 1.0。 |
| 5. 检索命中 | A 侧 **missing**（未封存）。B 侧 AB-017/018/019/020 分别命中目标模板 1/1/2/2 个候选；AB-019 的模板候选排第 2、3 位。 | 未执行生产查询。活动索引无真实说明，不能命中其正文。 | 未执行候选查询。活动索引无真实说明，不能命中其正文。 | 未运行真实检索。预览 chunk 的存在只证明可索引，不证明排序或召回。 |
| 6. 实际送模 | A 侧完整输入包 **missing**。AB-018 已发布 1 个外协管理办法引用；AB-019 已发布 2 个其他文档引用；AB-017/020 无引用、未发布。引用验证代码要求引用来自已送材料，可据此判断 AB-018/019 的这些引用材料已送模，但无法重建完整包。B 侧也未封存送模包。 | 未执行模型调用。 | 未执行模型调用。 | 未执行模型调用；不能声称答案已修复。 |

抽查段落以原件 XML 顺序的原始短段为单位，SHA-256 分别为：填写目的 `2ce7852bfca6`、背景 `b3e27e2d3b84`、相关方 `3ce702b04a26`、SMART 示例 `de9764813e43`、成功标准示例 `5ffd83fca23a`、范围边界 `c833c9bb7595`、资源标题 `a9d40ee98e45`、硬件标题 `19a2c118c320`、环境标题 `bb3a87125050`、供应商标题 `62c3fdc62777`。前缀仅用于定位；完整摘要可由受控原件逐段计算。活动提示 chunk 在两个库均无上述段落。

历史 B 侧 AB-019 的两个模板候选 ID 为 `8ed38207-145b-4a74-890c-ad3e4718b99f`、`254bcd42-dd7e-41d5-a670-ae01f5e065ff`；候选内容 SHA-256 为 `484d61a5fcfa0b525948cbc7642d6f83aa4085ca29e84a6b90b730b5425c8ee1`、`8cc5fbeb99a9d6878ec3d1d2c7a011e25a25981122d0854d14cd91dc646febb7`。这是 B 侧命中，不能移植为 A 侧命中。

## 根因与来源绑定

旧 `template_catalog.py` 对路径中任意“模板”段统一生成目录提示 DOCX；旧 `WanshitongTemplateParser.parse` 虽先调用安全 DOCX Parser，却丢弃其真实 IR，重建仅含目录提示的阅读 IR。两处都会排除填写说明。当前 18288/8289 活动源又是不同于 A/B 原件的提示 DOCX，单改代码无法让旧活动版本恢复正文。具体哪次操作把提示 DOCX 上传为活动源，现有证据不能确定。

当前 18288/8289 的受信显示名、`document_title` 均为 `1-项目启动-XXX项目软件研发管理计划20260612[.docx]`；受信 `source_relative_path` 指向 `04 开发中心/01 OPC模式开发流程模板/` 下的同名文件。元数据没有登记 `trusted_aliases` / `document_aliases` / `source_aliases` / `aliases`。现有 `_resolve_mention("研发管理计划", catalog)` 只做规范化后精确比较，结果为 **UNRESOLVED**。AB-019 对《研发管理计划》的显式点名因而还需通用的受信别名登记与唯一性校验；不能靠去日期、去前缀或文件名特判来“猜”别名。A/B A 侧的实际解析结果未封存，不能断言当时唯一失败原因就是此处。

## 已做修复、版本与重建界限

本地代码把 DOCX 上传保持为原件字节，让安全 Parser 的真实 IR 继续进入阅读视图；旧不可解析 `.doc` 的目录存在性产物仍由 `catalog_legacy_template.py` 独立生成。Parser 描述符由 `wanshitong-template-source-v2` 提升为 `wanshitong-template-source-v3`（当前离线 `3+00a40f5a73d9fe70`），将策略变化带入索引身份。保留的“例如”是例子语义标记，`XX` 是占位文本；它们可用于说明模板写法，但不构成已完成项目事实或企业统一指标。最终答复仍须由支持关系检查执行这层约束。

受影响的当前活动版本是 18288 的 `dver_52704274b128fa2dd6a7e9500597cf67` 和 8289 的 `dver_d7bbaec69cdecc2a5282d0400876ee85`；两者源摘要均非原件，不能在原版本上伪造恢复。若后续 P0 门禁解除并获准做候选验证，应仅在隔离候选库以 `c2da…d40e1` 原件建立**新文档版本与候选索引修订**，核对原件 SHA、197 节点、目标说明阅读域与 chunk、实际指定源命中和送模包，再考虑发布。生产 18288 保持现状，旧已退休原件修订不直接激活。当前阶段因支持复核 P0，未部署、未重建、未做真实模型回放。

## 只读复查入口

1. 本地原件：`sha256sum '/mnt/c/Users/jerry/Desktop/湾研院/湾事通/RAG知识库目录结构+文档/04 开发中心/01 OPC模式开发流程模板/1-项目启动-XXX项目软件研发管理计划20260612.docx'`。A/B ZIP：`sha256sum '/mnt/c/Users/jerry/Desktop/湾研院/湾事通/借鉴/3湾事通_原版WeKnora隔离AB与取舍方案/AB实测结果_20260925.zip'`；使用 Python `zipfile` + `tarfile` 只读打开嵌套 `AB完整受控证据.tar.gz`，查看 `corpus-evidence/DOCX-028.docx`、`sources.private.ndjson`、`review/diagnostics.private.jsonl` 与 `run-development/AB-0{17,18,19,20}-T1-{A,B}.sse`。输出只保留摘要、ID 和状态。
2. 60 主机容器内使用 Python `sqlite3.connect('file:/data/universal-rag.sqlite3?mode=ro', uri=True)`。实际服务容器 `wanshitong-wb08r-root-production-app` 发布在 60 回环 `18290`；候选容器是 `wanshitong-sso-candidate-app`（回环 `8289`）。另一个 `wanshitong-wb08r06-production-app` 仍在运行，发布在 60 回环 `18288`，本次不将它作为用户服务身份，也不将其视为可清理资源。按 `documents.display_name` 定位后，查 `documents.current_version_id`、`document_versions.content_sha256`；通过 `revision_documents.document_id/revision_id` 与 `index_revisions.index_revision_id/state` 查活动 `document_ir_json` / `chunk_count`；在 `chunks` 表按 `revision_id + document_id` 查 `citation_text` 并本地计算 SHA-256。所有查询保持只读；不导出原文。
3. 离线代码门禁：在 `/home/jerry/work/RAG-cq1` 运行 `PYTHONPATH=/home/jerry/work/RAG-cq1/src:/home/jerry/work/RAG-cq1 /home/jerry/work/RAG/.venv/bin/python -B -m pytest -q tests/wanshitong/test_template_catalog.py tests/wanshitong/test_template_parser.py tests/wanshitong/test_template_source_lifecycle.py tests/wanshitong/test_legacy_template_catalog.py tests/wanshitong/test_docx_bulk_import.py`；实测 16 passed。所改 Python 文件的 `ruff check` 实测通过。该门禁验证结构变体、原件摘要、来源跨度、阅读域、预览 chunk、离线完整上传与重启后的持久化，以及旧 `.doc` 兼容。离线 harness 未配置活动检索档案，直接 `sdk.search` 返回 `CONFIGURATION_REQUIRED`；故此门禁**不是**实际 8289 检索或模型质量门禁。

审计文档位于被 `.gitignore:16` 忽略的 `docs/` 下，交付提交时须明确 `git add -f docs/wanshitong/cq1/TEMPLATE_DATA_AUDIT.md`。
