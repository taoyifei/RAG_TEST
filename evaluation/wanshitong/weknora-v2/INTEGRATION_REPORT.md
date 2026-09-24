# WeKnora V2 隔离接入记录（2026-09-24）

## 范围与版本

- 本方基线：`codex/wb08r-q1-algorithm-debt` 的 `e3e664cf4fdda16ef030233261a22587e556e848`。
- 上游固定：Tencent/WeKnora `1edcd54b43606d9079bb36650efe3f68707a79ea`。本方 Go 分块二进制 SHA-256：`491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f`。
- 实测应用源码修订：`7681051d5e047c651f9dd78125497231c94366cb`。前两轮应用镜像 ID 为 `sha256:3fa9153b742eeee8b5db6c3663fbaa0b7703d9f870a12b644cc65c1c5ffd9619`；WP03 应用镜像 ID 为 `sha256:b862844d2a55eaf1e45cc94d3d7ec10a9702839d588a29926ee4e69f86a83811`。
- WP03 docreader：Linux amd64 `wechatopenai/weknora-docreader@sha256:546aa7902310144e851a6e8dfd5a3e713aca4e7ca4456782a1efc69e7359708b`，镜像标签上游修订与固定提交一致。仅在隔离 Docker 网络中提供 gRPC 50051，不发布主机端口。
- 测试仅在 60 服务器的 8289 候选端口进行。原 8289 容器先停止，隔离副本使用独立数据目录、Qdrant 和 Docker 网络；18288 容器、镜像及挂载目录未改。

## 实际贯通证据

| 链路 | 实际结果 | 证据 |
| --- | --- | --- |
| 旧索引上的新自然问答链 | `wk-standard-v1` 返回“采购文件发布到应答截止至少要 3 日 [S1]”，1 条引用，HTTP 200。 | Trace `trace_68b6af3da0b546c4a65efd91445fb9cf` 为 `ANSWERED`；包含 `retrieval.weknora_engine`、`retrieval.weknora_generation`、`provider.generation`，没有进入旧逐 Claim 发布链。此时 Embedding/Reranker 主机别名尚未补齐，故不把这次当成完整 Hybrid 成功。 |
| 固定上游 Go 分块与父级回读 | 合成 Markdown 的版本 Job `job_d0422cb04561fc2d349c0e3a9879351c` 完成并激活 `irev_a5f7c369d9b716c0b1409ea49268b5dd`。该修订有 13 个子块、1 个父段，11 个子块指向该父段，向量覆盖 13/13。 | 活动 Revision 的 `chunker_identity_json` 为 `weknora-adaptive-parent-child-v1`、固定上游 SHA 与 Go 二进制摘要；候选问答 `wk-standard-pc-v1` 返回带 `[S1]` 的字段答案。Trace `trace_5d901f6ac5eb476195ff49c8a1e4fd10` 为 `ANSWERED`，引用 `S1` 覆盖 11 个子块，`source_complete=true`、`citation_basis=parsed_artifact`，证实实际回读父段。 |
| 内网检索与生成模型 | 上述父子问答 Trace 中 `dense:primary` 有 13 个命中，`selected_embedding_slot=primary`，`RERANK_EXECUTED` 且 `mode=provider`，`provider_call_count=3`。 | 同一 Trace 包含 `provider.embedding.query`、`provider.reranking`、`provider.generation`，无降级原因。 |
| 新格式 MD/TXT | Markdown 和 TXT 均经管理员上传入口完成解析、分块、索引、检索及带引用问答。 | TXT Job `job_9c2d1066d781c28d214750eff2387fd9` 完成后，活动 `irev_4765043c1e94919aadd0608a1888c573` 有 14 个子块、1 个父段、14/14 向量覆盖；TXT 问答 Trace `trace_7f548bad3824473fb336856e8b817d0c` 为 `ANSWERED`，引用指向 TXT 文档且基准为 `parsed_artifact`。 |
| PPTX | 固定 docreader 的 MarkItDown 引擎抽出幻灯片文字；上传 Job `job_181fec0de10d8d0bb22896b8a6df848d` 成功。问答返回“PPTX-0924 的批准期限为七天 [S1]”。 | Trace `trace_ff747a2a80bc44f68702db38dff00005` 为 `ANSWERED`，引用指向该 PPTX 的版本与 `parsed_artifact` 来源。原文件 Artifact HTTP 下载为 200，大小 28,327 字节。 |
| XLSX | 固定 docreader 的 builtin 引擎抽出表头与数据行；上传 Job `job_f1991625a350f560ed29c80f0fa77579` 成功。问答返回“XLSX-0924 的预算额度是八十万元 [S1]”。 | Trace `trace_7733f87cd4a74808ac00747255f8d3b8` 为 `ANSWERED`，引用指向该 XLSX 的版本与 `parsed_artifact` 来源。原文件 Artifact HTTP 下载为 200，大小 4,929 字节。 |
| CSV | 固定 docreader 的 MarkItDown 引擎保留表头和值；上传 Job `job_e7a203d4b541144e44315c26ee82e2ccb` 成功。问答返回“CSV-0924 的设备数量是十四台 [S1]”。 | Trace `trace_bb656028759a4d31aad250b199a6e810` 为 `ANSWERED`，引用指向该 CSV 的版本与 `parsed_artifact` 来源。原文件 Artifact HTTP 下载为 200，大小 41 字节。 |

上述五份文档均为隔离测试合成材料。WP03 最终活动 Revision `irev_172b7fb2ceee864a779297c1c5d2350a` 包含 3 份新增格式文档、4 个 Chunk，独立 Qdrant 中该 Revision 为 4 个向量点。管理员 Overview 的格式探测为 `available`，`md/txt/pptx/xlsx/csv` 均为 `enabled`。三个新增格式的 Trace 都包含 `provider.embedding.query`、`provider.reranking`、`provider.generation`、`RERANK_EXECUTED` 和 `dense:primary`，证实真实模型参与。问答正确性只说明这些样本链路贯通；没有用 F017 或合成样本代替真实语料质量评估。引用验证等级为 `citation_binding_only`。

## 代码与构建门禁

- 相关 Python 定向测试：32 passed；修改范围内 Ruff、mypy 与格式检查通过。
- 上游 Go 分块测试、离线构建及二进制摘要核对通过；前端 `npm run build` 通过；候选 Docker 镜像构建与资源自检通过（43 个资源文件）。
- 扩展运行的 `tests/application/test_profile_impact.py` 有 1 个失败：测试硬编码旧 `CATALOG_VERSION=2026-09-14.2`，当前为 `2026-09-14.3`；在未修改的 Q1 基线上可复现。本次未为制造通过而修改该无关断言。未运行全仓测试。

## 发现与限制

1. 原 8289 数据副本的 46 份活动文档有 61,522 个旧 Chunk，但原容器和隔离副本的来源 Blob 目录均为 0 个文件。旧文档重建返回 `INVALID_DOCUMENT`。本次只在隔离副本中处理测试数据，真实语料迁移前需恢复原件，详见 [P0-8289-source-artifacts.md](P0-8289-source-artifacts.md)。
2. PPTX、XLSX、CSV 已经逐格式真实贯通，但 docreader sidecar 在测试后按要求移除；当前恢复运行的原 8289 镜像不含本次候选代码，也没有启用这些格式。DOCX 默认路径保持原行为。复杂图表、扫描件 OCR、多 Sheet 及真实业务语料准确度未在本次正例中验证；不能从三个短样本推断这些能力已通过。
3. 候选打包曾遇到服务器旧 Docker builder 不支持 `COPY --chmod`，已改为 `COPY` 加 `RUN chmod`；该 builder 还不识别 Dockerfile 专属 `.dockerignore`，WP03 只在临时构建上下文移除了 `frontend/dist` 排除规则。临时网络曾与 SSO 地址段重叠，已改用 `192.168.240.0/24`；容器缺少 `host.docker.internal` 映射造成模型连接验证失败，补齐后 Embedding、Reranker 的实时验证均成功。WP03 新建私有诊断目录须为 `0700`，修正临时目录权限后应用健康。这些变更只影响隔离部署配置，未修改生产容器。
4. 真实语料的 A/B/C 质量比较及并发性能测量本次未运行：原 8289 数据副本缺来源 Artifact，无法以同一批原文件构建父子候选索引。功能接入已通过；质量改善、生产可用性和发布批准均未据此判定。

## 清理与运行状态

- 测试结束后移除本次 `wb-v2-7681051-*`、`wb-v2-wp03-*` 容器和网络、独立数据副本、归档、候选应用镜像与 docreader 镜像；本机下载的 docreader 镜像、合成样本和临时构建文件也已清理。没有运行全局 Docker prune。
- 原 `wanshitong-sso-candidate-app` 已恢复，容器 ID `14c0f9a42c1479ef9c272ef4ff9f0c352c3855d292e9a5c9821f3021c0fdb591`、镜像 ID `sha256:ca38110829dd5a1e65589e9571744a1c7342b3b2fc95e94004ba9f7aa9168d06` 均与测试前一致；健康状态为 `healthy`。受保护生产应用仍是原容器 ID `fb65fdaeaec49c161bc6a7e73c9af7eca01cb9c5858341c073c75a64d64abf47`、镜像 ID `sha256:96f1aa48eed69de4a98f48af6f4fa89ebf5b35a6392c30697ee855b105f5ecd9`，健康状态为 `healthy`。
- 从本机访问 54 代理的 `/kb/`：8289 与 18288 均为 HTTP 200。未在 18288 发布候选代码。
