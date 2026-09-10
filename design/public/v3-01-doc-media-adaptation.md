# V3-01 DOC 图文保留与默认 Product 闭环

日期：2026-09-08

公开性：本文只保留公开夹具和匿名聚合结果，不包含私有原件、文件名、正文、
逐文件 hash、凭据或本机路径。

V3-01 的源码、离线回归、最终候选镜像、真实 DOC 转换、默认 Product 上传/
重启/媒体回读均已完成。二进制 DOC 现在可以在普通上传路径中经过受限
LibreOffice 转换，复用现有 DOCX Parser、DocumentIR 和结构化 Chunker；原生
DOCX 仍直达既有 OOXML Parser。

发布安全门保持 `BLOCKED`：最终镜像的完整 Trivy 扫描有 79 个无可用修复的
High/Critical 包版本元组，且现有人工审查绑定旧镜像，不能移植批准状态。
因此正式 `release acceptance` 为 `NOT_RUN`，没有发布、push、merge、生产
部署或真实 Provider/OCR 调用。

## 状态

| 状态项 | 结果 | 依据 |
| --- | --- | --- |
| `ENGINEERING_DONE` | `PASS` | DOC 转换、清洗、身份映射、Artifact/MediaScan/持久化和兼容 fallback 已实现 |
| `DEV_STAGE_GATE` | `PASS` | compileall、Ruff、363 个源文件 mypy、docstring 检查通过；2674 passed、89 deselected |
| `BUILD_CONTEXT_GATE` | `PASS` | 干净 HEAD 的 4.79 MB 白名单 context 完成 BuildKit 构建和 Compose config 校验 |
| `DOC_CONVERSION_SANDBOX` | `PASS` | 非 root Product、Landlock、seccomp、rlimit、隔离目录、进程组终止及网络拒绝均有实际证据 |
| `DOC_MEDIA_PIPELINE` | `PASS` | 公开真实 DOC 完成 Parser → IR → Artifacts → Chunk → Report；长表和重复图片实例通过 |
| `DEFAULT_PRODUCT_MEDIA_E2E` | `PASS` | HTTP 上传、幂等、激活、Artifact/MediaScan/预览、容器重建和冷启动回读通过 |
| `PRIVATE_LOCAL_REPLAY` | `PASS` | 10/10 份本地私有 OLE DOC 完成直接解析及默认 Product 入库；只保留匿名汇总 |
| `NATIVE_DOCX_AND_QUERY_REGRESSION` | `PASS` | 原生 DOCX、V3-00.6 入库合同及 V3-00.7 语义问法均包含在最终全量门禁中 |
| `RELEASE_SECURITY_GATE` | `BLOCKED` | 79 个 High/Critical 元组无修复；新镜像没有匹配且已批准的逐项风险处置 |
| `RELEASE_ACCEPTANCE` | `NOT_RUN` | 上游安全门阻断后按失败即停未执行 |
| `LIVE_QUALITY_EVIDENCE` | `NOT_RUN` | 未调用真实 Embedding、LLM、OCR 或视觉模型，不外推检索/回答/图理解质量 |
| `PUSH_MERGE_DEPLOY` | `NOT_RUN` | 未获授权；`main`、`Industry`、远端和生产服务未修改 |

## 范围与身份

- 执行分支：`codex/universal-selective-word-v3`。
- V3-01 起点：`8ceb2f5`；最终候选源码提交：
  `b5f8ab4fc14106168b1995e4df57c1e866e6f2b5`。
- Industry 只读参照固定为
  `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`；没有整体合入旧 Runtime。
- 最终镜像 tag：`docx-rag:v1-candidate`；本地 image ID/RepoDigest：
  `sha256:d3ba93da961b259c6c6db19d7c819dc69813b84e7e424680c1448fa69a93975b`。
- OCI revision 和镜像内 `_build_revision` 均精确等于最终源码提交；基础镜像仍为
  固定 digest 的 `python:3.11-slim-trixie`。
- 最终 build context manifest 为
  `artifacts/v3-01/build-context-manifest-final.json`，405 个文件、4,792,950
  bytes，SHA-256
  `d5c191524375252195d9c7ae4a0f0f626284f379131e30b501649c8a68510ae6`。
- Product 实际报告 Parser 版本为
  `2.0.1+docx-4.0.0.libreoffice-landlock-seccomp-v2.antiword-0.37-17`，
  index fingerprint 为
  `sha256:eaad5da1ea246987e028a889f928636421bde7b35e32c5a71cc7768c568d1a75`，
  serving fingerprint 为
  `sha256:68a5ee749b86ff2aba346a7d84c5a55742cd6b8a1c8f107d7a126487613a5f94`。
- 私有 DOC 原件、文件名、正文、图片、转换件、逐文件 hash 和原始报告没有进入
  Git、build context 或本文件。

## 根因及 Industry 对照

V3-00 的 DOC 路径仅调用 `antiword`，能保留可用纯文本，但在转换前就丢失表格、
图片实例和派生 OOXML 结构；因此文本 coverage 为 1.0 也不能证明图文完整。

Industry 参照中的 `_prepare_document` / `_run_converter` 会把 DOC 原始字节直接
交给固定 LibreOffice，随后清洗并解析生成的 DOCX。V3-01 首版在此语义上增加
了安全沙箱和 OLE 前置验证，但错误地要求整个 CFB 文件长度必须是 sector 大小
的整倍数。实际私有回放首轮 8/10 成功；两份合法 OLE DOC 分别有 251 和 54
个不参与目录链的尾部字节，因而在进入 LibreOffice 前被误拒绝。

修复只删除这一错误的整文件长度约束。OLE magic、字节序、sector 参数、DIFAT/
FAT、目录链和 `WordDocument` stream 校验仍保留；完整原始尾部继续交给受限
LibreOffice。修复后同一私有集合直接解析和 Product 上传均为 10/10。

这解释了“Industry 能而首版通用路径不能”的具体差异，但不把 10 份本地样本
外推成“所有历史 DOC 方言均可完美解析”。损坏 OLE、密码保护、无法安全转换的
嵌入对象及目标环境缺组件仍会明确失败或走受控 fallback。

## 最小实现

### 格式路由与转换

1. `.docx` 保持 `word-document-v1` → `docx-ooxml-v4` 的直接委托，不经过
   LibreOffice；`.doc` 才进入 OLE/Word stream 校验和转换路径。
2. 扩展名、MIME 与真实签名共同决定路由。RTF、HTML、伪 DOCX、非 Word OLE、
   损坏 FAT/目录结构均被明确拒绝，不能靠改后缀触发转换。
3. 目标镜像固定 `LibreOffice 25.2.3.2`、Debian package
   `4:25.2.3-2+deb13u6`、`antiword 0.37-17`、Noto CJK 字体和
   `libseccomp2 2.6.0-2`。下载失败只重试同一固定版本。
4. 转换不可用时才显式回退到既有 `antiword` 扁平化路径，并继续报告
   `unknown_after_flattening`；转换已启动后的失败、超时或安全合同失败不会被
   fallback 掩盖。

### 转换隔离与派生清洗

- 主应用仍为 UID/GID 10001，未获得 Docker socket、privileged、宿主根目录或
  生产 Secret。每次转换创建独立 input/output/profile/temporary 目录；输入只读，
  只接受唯一普通 `source.docx` 输出。
- runner 以 Python isolated mode 启动，先设置 `no_new_privs`、地址空间、CPU、
  文件大小和进程数限制，再应用 Landlock 与 seccomp，最后 `execve` 固定
  LibreOffice argv。全局并发上限为 2；排队和运行共用 ParsingPolicy wall
  timeout；取消、超时和工作区超限都会终止整个进程组并清理临时目录。
- Landlock 只允许读取固定运行库、字体、配置和输入；只允许输出/profile/
  temporary 的必要写入。`/tmp` 仅允许 LibreOffice 所需 AF_UNIX socket，禁止
  普通文件写入。seccomp 拒绝 IPv4/IPv6 socket、mount、ptrace 等系统调用。
- LibreOffice profile 显式禁用宏执行和更新检查。派生 DOCX 再次执行 ZIP 数量/
  大小/压缩比/路径/XML 限制，拒绝 macro、VBA、OLE、ActiveX、嵌入包、DTD 和
  entity；所有外部 relationship 及其引用在解析前移除，不访问目标。

### 身份、Artifact 与来源链

- `Document ID`、`DocumentVersion`、source SHA 和 source Artifact 始终绑定原始
  DOC。转换后的清洗 DOCX 是 `derived_document`，图片是
  `embedded_media`，均保存实际独立 hash；派生 ZIP 时间戳固定，不回写原始身份。
- 派生 OOXML 节点重新映射到原始 DOC version，并在 part URI/structural path
  标记 `converted-docx`。SourceSpan、节点父子关系和 relationship 都使用映射后
  ID，不留下指向临时 DOCX version 的悬空引用。
- 来源定位质量明确为 `converted_instance`，不把派生页码或 OOXML part 冒充
  原始 DOC 精确坐标。`native_text`、`media_inventory`、图片实例数、Artifact
  数和关系状态分别记录；图片未知总数不会写成 0 张已确认。
- 相同图片字节共享 blob/content identity，但每个显示位置有独立 node、anchor
  和 occurrence。Artifact lifecycle、Revision 校验和 SQLite schema 已允许并
  复核 source/derived/embedded 三类角色及引用计数。
- 纯图 DOC 即使没有可分块原生文字，也能保存图片 Artifact、完成 MediaScan 和
  权限预览；本阶段不把图片本身、`[pic]` 或文件名解释为图内事实。

## KEEP / REPAIR / ADAPT / DEFER / UNVERIFIED

| 能力 | 决策 | 结果与边界 |
| --- | --- | --- |
| 原生 DOCX OOXML Parser 和结构化 Chunker | `KEEP` | 直达既有实现；正常内容、顺序、三视图、SourceSpan 与语义问法回归通过 |
| 旧 DOC 的 antiword 文本降级 | `KEEP` | 只作为组件不可用时的显式 fallback，不再冒充图文完整解析 |
| DOC 表格、图片及派生来源链 | `REPAIR` | 受限转换后进入当前 IR/Artifact/Chunk/Report/持久化链 |
| DOC 转换格式与安全前置校验 | `REPAIR` | 删除错误 sector 整文件约束；保留真实 OLE/Word stream fail-closed 校验 |
| Industry 的 LibreOffice 转换语义 | `ADAPT` | 只复用已核实行为，改造成通用无业务状态的受限 adapter，不搬旧 Runtime |
| Product Artifact、MediaScan、权限预览 | `ADAPT` | source/derived/embedded 角色贯通上传、冷启动和回读 |
| OCR、图关系和视觉事实 | `DEFER` | 属于 V3-02；媒体 PASS 不等于 OCR 或关系正确 |
| EMF/WMF/SmartArt 专用转换平台 | `DEFER` | 本阶段只保留 LibreOffice 实际产出的安全媒体，不扩成万能格式系统 |
| 原始 DOC 媒体总数与像素级版式一致 | `UNVERIFIED` | 转换结果只能标记 partial/unknown 和 converted-instance 定位 |
| 真实 Provider、检索/回答质量和生产容量 | `UNVERIFIED` | 未获数据出网、预算或生产部署授权 |

## 实际链路证据

### 公开真实 DOC

公开夹具先由 LibreOffice 生成真实 OLE DOC，包含 121 行/242 单元格长表、正文和
同一图片的两个显示实例。最终镜像内实际转换结果：

- 原始 DOC source identity 保留，派生 DOCX hash 与原始不同；
- Artifact 角色为 source/derived/embedded；同一媒体 Artifact 对应两个独立
  image node/anchor；
- IR 有 121 个 table row、242 个 table cell，Chunk report 全部表示；
- 28 chunks，source span coverage 1.0，missing source chars 0；
- mapping quality 为 `converted_instance`，media inventory 为 `partial`，
  relationship status 为 `derived_internal_only`。

一次把公开长表、私有批量解析、安全探针和身份探针同时启动的附加压力运行中，
公开 LibreOffice 子进程曾非零退出一次；同一命令单独重跑通过，随后两个隔离
公开转换容器并行也同时通过，Docker 事件没有 OOM/kill。该瞬时事件没有被隐藏，
但未能复现为代码缺陷；正式门禁和最终 Product 链路均已重新运行通过。

### 本地私有匿名汇总

最终镜像只读挂载本地私有输入并禁用网络。直接 Parser/Chunker 汇总为：

- 10/10 成功，原始身份 10/10 保留，派生制品 10/10 独立；
- 300 chunks，最小 source span coverage 1.0，missing source chars 0；
- 10 个 source、10 个 derived、1 个 embedded media Artifact；
- 1 个独立图片实例；media inventory 为 1 个 partial、9 个 unknown；
- IR 记录 24 个 table row、138 个 table cell，报告表示其中 19 行/88 个可引用
  单元格；不把空白或非可引用单元格制造成正文；
- relationship status 为 9 个 `derived_internal_only`、1 个
  `external_removed`。

默认 Product 隔离网络中再次上传同一 10 份输入，10/10 Job succeeded、10/10
source identity 保留、10/10 derived Artifact 存在；含媒体文档的 1 个
MediaScan 与 1 个权限预览逐字节摘要一致，失败阶段为 0。没有输出逐文件名称、
正文、hash 或图片。

### 默认 Product 冷启动

公开 DOC 通过真实 HTTP/Session/CSRF/Idempotency-Key 上传；Job attempt=1 且
succeeded，重复 Idempotency-Key 返回同一 Job。活动 revision 建立后可下载原件、
派生 DOCX 和图片，MediaScan count=1，图片 Content-Type 为 `image/png`。

随后完整删除并重建应用容器，保留同一数据目录和 Secret。新 Session 登录成功；
活动 revision、三个 Artifact 的数量与字节、媒体字节和 MediaScan 均保持稳定。
该运行使用内存 Qdrant、离线确定性组件，OCR/Provider 调用为 0。

### 沙箱实际访问探针

最终镜像在 `--network none`、read-only rootfs、`cap_drop ALL`、
`no-new-privileges` 下验证：input 可读、专用 output 可写、AF_UNIX 本地 IPC 可用；
应用锁文件、挂载 Secret、`/tmp` 普通文件、IPv4 和 IPv6 socket 均被拒绝。

## 最终命令与结果

| 命令/动作 | 退出状态 | 结果 |
| --- | ---: | --- |
| `.venv/bin/pytest -q`（Word、转换、V3-01 E2E、Product Runtime） | 0 | 34 passed、1 个既有 Starlette/httpx warning |
| `.venv/bin/python scripts/dev.py check` | 0 | compileall、Ruff、363-source mypy、docstring 通过；2674 passed、89 deselected、4 warnings，718.43s |
| `.venv/bin/python scripts/release.py build --context-manifest ...` | 0 | BuildKit 和 Compose config 通过；source revision 与 manifest 均绑定最终提交 |
| 最终镜像 `build-info --expected-revision ...` | 0 | installed/expected revision 完全一致 |
| 公开 DOC Parser→IR→Artifact→Chunk→Report 容器探针 | 0 | 长表 121/242 全表示；2 个显示实例共享 1 个媒体 Artifact；28 chunks |
| 两个隔离公开 DOC 转换容器并行运行 | 0 / 0 | 两份均完成转换、结构映射和完整性断言 |
| 私有 DOC 直接 Parser/Chunker 匿名回放 | 0 | 10/10 成功；300 chunks；来源与 Artifact 聚合通过 |
| 默认 Product initial/restart/private 三段 HTTP 驱动 | 0 / 0 / 0 | 公开首次、容器冷启动、私有 10/10 和媒体预览均通过 |
| 最终镜像 Landlock/seccomp 访问探针 | 0 | 允许最小本地 IPC；拒绝 Secret、应用文件、普通 `/tmp` 写入和 IP socket |
| `scripts/release.py verify --evidence-file artifacts/v3-01/evidence.json ...` | 1 / `BLOCKED` | pip/npm 0 漏洞、runtime 工具边界、1425-file Secret scan、SBOM/license 完成；OS 风险门阻断 |
| `scripts/release.py acceptance` | `NOT_RUN` | 安全门阻断后未执行 |
| 真实 OCR、Embedding、LLM、视觉理解、生产部署 | `NOT_RUN` | 无对应出网、预算或生产授权 |

89 个 deselected 用例来自显式 `local_integration` / `live_provider` 边界，不包含
本阶段新增必需测试。4 个 warning 是既有 Starlette/httpx 弃用提示及合成 Qdrant
连接/version 提示；没有改成 skip、xfail 或宽松断言。

## 发布扫描与证据

最终 `evidence.json` SHA-256 为
`df7ee801a41294f3ffa07cf7acbd8e22e74647be39b8e829876c8f6997a5cfb1`。
最终安全目录为 `artifacts/v3-01/security/20260908T115201579476Z`：

| 证据 | SHA-256 |
| --- | --- |
| `trivy-all.json` | `8dc24db770f34ad426dfd7e8e8b71b39c1d63e7c815e70b50204c27abd6579e9` |
| `trivy-fixable.json` | `6f78a8bf951a59fccd0e6a2db9b6e0ca646c207945cc42c0ce2c38b2af2df0ba` |
| `sbom-image.cdx.json` | `9e4f42fbebedf053fe438d88b9025fbfb9e15629425b13f5c633eac5e1896750` |
| `licenses-image.json` | `ef1ac3c8ebf82dc5e120f9f6c1b9dd561f4584aec4239fd21a4c723936e555b5` |
| `os-risk-review.json` | `184879d50c6e0a45666c5d3ed317ec92101888670fc627da7d5f254ef7e96106` |

完整扫描有 425 条原始发现；High/Critical 为 79 个唯一 CVE+package+installed
version 元组、37 个唯一 CVE，`FixedVersion` 非空为 0。79 项均保持
`UNDER_INVESTIGATION`，批准处置为 0。现有
`release/p11-os-risk-review.json` 绑定旧 image ID，最终核验明确返回
`REVIEW_SCAN_IDENTITY_MISMATCH` 和 `REVIEW_SCAN_IDENTITY_UNAVAILABLE`，没有
移植旧批准、伪造人工签名或用 `--ignore-unfixed` 子门替代完整风险门。

### 未决发布问题 V3-01-SEC-01

状态保持 `BLOCKED`。Industry 固定参照没有可直接移植的解决方案：其应用镜像
使用 `python:3.11.13-slim-bookworm`，并未把 LibreOffice/antiword 转换组件装入
应用容器，也没有 V3 当前的 Trivy、SBOM、镜像身份绑定人工审查闭环。按本次
相同 Trivy 0.74.0 漏洞库对该固定 Bookworm 基础镜像 digest 单独复扫，共得到
98 个 High/Critical 元组，其中 38 个已有可用修复、60 个无可用修复。该结果
只是基础镜像对照，不是 Industry 完整应用镜像的发布证明；它既不能替代当前
候选镜像扫描，也不能满足 V3-01 的旧 DOC 图文转换合同。

Industry 可供后续设计借鉴的是把高风险转换面从主应用容器拆成独立、最小权限
worker，但这只会缩小主应用攻击面，转换 worker 仍须独立生成 SBOM、完整扫描并
绑定自身 image ID 完成人工风险审查，不能绕过门禁。本问题只有在以下任一条件
成立后关闭：上游提供修复版本并重建候选镜像，使完整扫描通过；或授权审查人对
当前精确 image ID 的 79 个无修复元组完成逐项、未过期的风险处置。关闭后必须
重跑 `release verify`，通过后才允许执行 `release acceptance`。

## 版本与重建范围

`word-document-v1` 从旧 antiword-only 身份升级到 2.0 系列，当前最终版本又把
实际 `libreoffice-landlock-seccomp-v2` recipe 与 descriptor 绑定，防止报告和
index fingerprint 漂移。DOC 内容语义、Artifact 角色和来源链已改变，不能把旧
DOC revision 改标签继续使用；部署时必须走“构建新 revision → 完整校验 → 原子
激活”，失败保留旧 active revision。

内容层面只有 DOC 需要新转换；原生 DOCX 解析和 Chunk 语义未改变。但当前
Product 把统一 Parser descriptor 绑定在 KB 级 index fingerprint，而不是按文档
格式拆分，因此运行时会如实把旧 fingerprint 的 KB 标记为需重建，即使该 KB
恰好只有 DOCX。不得为避免重建谎报兼容。后续若要缩小这一运维影响，应另行设计
可审计的 per-format/per-document parser compatibility，不在 V3-01 中临时改写
现有指纹合同。

## 本地提交

- `2219f5a599c1472d39f13de7137e3b1ce8def2e5`
  `feat(ingestion): 保留 DOC 图文结构与派生制品`
- `5677171e4143470ef96eb0a63566ed9f9e01369c`
  `fix(ingestion): 修正 DOC 沙箱权限初始化`
- `47bb802a350d1330e772ff4082edd769efb3c74d`
  `build(image): 重试固定 Office 依赖下载`
- `e76f7ddbdbc2860cde435f6ed9c1c64a55e8a82a`
  `fix(ingestion): 兼容受限 LibreOffice 本地 IPC`
- `038c98dcbb423e624ae724ffbf3bfb87d684acd1`
  `fix(ingestion): 兼容非整扇区旧版 DOC`
- `b5f8ab4fc14106168b1995e4df57c1e866e6f2b5`
  `fix(ingestion): 对齐 DOC 转换配方身份`

本文件作为独立文档提交；实际文档提交 SHA 以最终 Git 历史为准。V3-01 到此
停止，不自动进入 V3-02；媒体链路通过不代表 OCR、图关系、真实回答质量或发布
安全获准。
