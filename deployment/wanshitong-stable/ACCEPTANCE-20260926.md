# 湾事通 × WeKnora 8289 候选验收记录

记录日期：2026-09-26。范围仅为 `54:8289/kb/` 经既有测试转发抵达 60 的独立候选栈。`54:18288`、`60:18288`、`60:18290` 和 WeKnora 原版 B `54:18391` 均未切换。

## 结论

| 判定 | 状态 | 依据与剩余条件 |
|---|---|
| ENGINE_READY | PASS | 固定 WeKnora v0.8.2 原生镜像；45 份当前唯一原件和 13 份历史原件进入原生 KB，真实解析完成、分块可见、原生问答有增量正文和引用。 |
| WRAPPER_READY | PASS | 真实 RDMS 登录、湾事通界面、白标管理页、原生 SSE、追问、历史、原件下载、反馈与 Trace 已在 8289 连通；网关不启动旧问答内核。仅表示测试候选的工程通路可用。 |
| CONTENT_ACCEPTED | FAIL | OPC 题出现未经引用支持的概念混同；其他业务题也有部分未获原文支持。见 P0-CONTENT-OPC 与样本表。 |
| RELEASE_READY | BLOCKED | 内容 P0 未关闭；共享生产模型的 1/2/4 并发与后台争用、跨用户真链路、离线恢复演练和指定 PDF/表格样本未完成。不得切换 18288。 |

## 版本、隔离和备份

- 分支：`wanshitong-stable`，起点 `dca24a80823b4e5d24dbbdd725e10c97167ad7a5`。上游源码为 `Tencent/WeKnora` v0.8.2，提交 `3e8b0bfc80b845b2d4b2ed683994748741450a97`；`vendor/weknora/frontend` 有白标和管理认证适配，原生 Go 与 Docreader 镜像未改。
- Compose 项目：`wst_weknora_candidate_20260925`，原生 app/docreader/postgres/redis/rerank、gateway/edge 使用独立卷和网络。原生 API 无宿主公开端口。候选 edge 只绑定 60 的 `127.0.0.1:8289`。
- 运行镜像 Image ID：app `sha256:bc8a534799fcb54045100daf4bc5db86270b38ae45c5f761c410c50b4dcd6cdc`；docreader `sha256:3c36e7e738ba515315523876d8041f9bb02809145a4e3b331e2550e4bff98b7a`；ParadeDB `sha256:58bf87d2a6f1e72f56590b0f0ae1467e82c3f99ca203dde646672c8ac13148b2`；Redis `sha256:c9d92d840fd011c908f040592857c724ae6d877f2aba5c40ad963276507386b2`；Python rerank adapter 基础镜像 `sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b`；gateway r6 `sha256:539954ecb771efdbbf4d9de1873837ee7b43126f8df2273ec16fcd1ad8b24825`；edge r6 `sha256:78546bf9d2227f9944e65287451ca876a7051f1af666aa8fb29fb928aa7d6598`。
- 60 的候选目录：`/data/tyf/wanshitong-weknora-stable-candidate-20260925`。离线镜像包 `backup/images/wst-candidate-20260926.tar.gz`，约 3.0 GiB，SHA-256 `18b4f3469e2d616d86b89840d03b70bfb4fae851d4b18f29f9fc1bb67ef0b218`。
- 同目录 `backup/native-candidate-final.dump`（`f13875954bf44d1667687245628d56b697091a66049e3f305cb631120a04c887`，`pg_restore -l` 可读）、`backup/native-files-final.tar.gz`（`4acc77c20b7c964770a9e1153ea9c5153c63ced3e4e5ac08954de3cb729b9ef8`）、`backup/gateway-final.sqlite3`（`a781b6989a5db4fb8213b56bb391f0b4f6625d65455742ee2722945b51e76363`，`integrity_check=ok`）。Secret 单独存于仅有管理员可读的 `backup/secrets/secret-files-final.tar.gz`，不进 Git/镜像包。切换前旧 8289 SQLite 只读快照位于 `backup/legacy-8289-precutover.sqlite3`，`integrity_check=ok`。
- 原 8289 容器 `14c0f9a42c14` 已停止但未删除；原镜像 `sha256:ca38110829dd5a1e65589e9571744a1c7342b3b2fc95e94004ba9f7aa9168d06` 保留。原测试转发、生产容器/镜像/卷均未作为清理对象。
- 60 上 `wst-weknora-sso-relay.service` 已启用并运行。受限 SSH 公钥只准 60 转发到 54 可达的 RDMS 验证地址；候选 gateway 经该中继使用真实 Ticket。服务重连与 HTTP 验证接口已检查，未做整机重启演练。

## 实际执行的工程检查

| 范围 | 结果 |
|---|---|
| Python 新网关 | `ruff check src/wanshitong_gateway tests/wanshitong_gateway`、`mypy --strict src/wanshitong_gateway`、`pytest -q tests/wanshitong_gateway`：38 passed。 |
| React | 变更范围 ESLint、`npm run typecheck`、`npm test -- --run`：236 passed；`npm run build` 通过。 |
| 白标 Vue | `npm run type-check`、14 个身份/路由/文件请求测试、`npm run build` 通过。 |
| 现场 | 七个候选服务运行，app/docreader/postgres/redis/rerank/gateway 健康；真实 8289 浏览器问答、管理端和下载已执行。七镜像离线包已在 60 上 `docker load`，加载后的镜像标签和 Image ID 与清单一致。 |
| 上游 Go | 未改 Go 代码，未运行 Go 测试。 |

## 真实业务样本与 P0

以下只对已展开的引用原文作人工判断，不把 HTTP 200、模型输出或旧答案视作正确性证明。资料版本为上述 45 份当前原件；13 份历史原件仅在私有归档 KB，不参与普通问答。

| 实际问题 | 结果 | 原文核查 |
|---|---|---|
| 研发经费包干制有哪些要求？及追问“它的负面清单包括哪些行为？” | 通路 PASS，内容未逐条评分 | 同一原生会话完成两轮、增量显示、5 段引用和历史刷新；原件下载 SHA-256 与迁移清单一致。 |
| 研发外协费用需要先有预算吗？ | 部分 | 原文支持“先有预算、后有支出”；回答又说签合同前须完成预算审批，该额外时点未由已看的片段直接支持。第一次触发模型 8192 上下文 400；原生候选把最大输出设为 2048、重排保留 3 段后同题完成。 |
| 2026 年火星矿业的差旅报销额度是多少？ | 无证据主结论正确，引用待改进 | 回答没有编造额度，明确说资料不足；原生仍给出 3 个不相关引用，包装层原样展示。 |
| OPC 模式是什么？它与普通研发项目开发流程有什么区别？ | **错误，P0-CONTENT-OPC** | 原生回答将“项目交付、需求快验、产品开发”三种工作模式归为 OPC 模式；其引用的《开发中心三种工作模式》片段只说明开发中心的三类模式，没有定义 OPC。原生分块中确有 7 段提到 OPC 团队/owner，仍不足以证明该概念等同三种模式。湾事通保留了原生正文和引用，未用旧规则改写。 |

P0-CONTENT-OPC 的恢复条件：补齐权威 OPC 定义或在原生 WeKnora 提示词/检索配置中消除这种概念混同，并用原文核验 OPC 大小写、跨文档缩写及“是什么/是啥”样本；随后再执行完整业务题矩阵。不能用湾事通壳层关键词规则遮盖。该问题属于原生内容质量/资料契约，非 SSE 转换缺陷。

## 验收矩阵逐项状态

`PASS` 仅代表所述范围的证据；`BLOCKED` 表示尚缺真实验收条件，`NOT_RUN` 表示未执行。

| ID | 状态 | 证据或原因 |
|---|---|---|
| B01 | PASS | 独立分支及实际 base SHA 已记录，原工作树未改。 |
| B02 | PASS | 上游 tag/源码锁定，原生镜像 digest 与现场 Image ID 一致。 |
| B03 | PASS | 网关原生 `/api/v1/knowledge-chat` 直通；无旧问答内核初始化或旧生成 fallback。 |
| B04 | PASS | 项目、端口、卷、网络和数据库隔离；生产入口未改。 |
| B05 | PASS | 上游许可保留；Secret 使用只读文件挂载，浏览器不持有原生 JWT；Git 不收录现场 Secret。 |
| N01 | PASS | 当前 45 份唯一原件及历史归档 13 份经原生上传任务完成。 |
| N02 | PASS | 管理页可见原生 chunks，真实搜索/回答引用到 chunk ID。 |
| N03 | BLOCKED | LLM、embedding、rerank 经问答链路工作；指定 OCR/PDF 样本未独立验证。 |
| N04 | BLOCKED | 已确认当前 LLM 8192 物理上下文和原生输出额度；embedding 维度/协议/超时完整实测未形成记录。 |
| N05 | PASS | 多个真实问题直接收到原生正文、事件与引用。 |
| A01 | BLOCKED | 真实 RDMS Ticket/验证码登录成功；到期和退出全链路仍需浏览器实测。 |
| A02 | PASS | 8289 已登记入口真实回调成功。 |
| A03 | BLOCKED | 已验证单个真实 RDMS 主体映射；第二个真实主体未获得。 |
| A04 | BLOCKED | 所有者校验有网关测试；无第二真实用户做跨用户浏览器探测。 |
| A05 | PASS | 管理单独 Cookie；未登录 Vue 进入湾事通管理登录，无腾讯二次登录。 |
| A06 | PASS | JWT 保留服务端，管理鉴权/拒绝路径与刷新/退出逻辑有针对性测试。 |
| S01 | PASS | UTF-8/JSON/换行任意字节边界回放。 |
| S02 | PASS | 原生 answer delta 与 raw event 保留，测试校验正文不改写和事件顺序。 |
| S03 | PASS | 局部 done 不当 complete，EOF/错误回放为失败；真实模型 400 未伪装完成。 |
| S04 | PASS | Trace 记录原生 session/message/request ID；真实反馈对应 native message。 |
| S05 | PASS | 重试不再生成 POST，浏览器刷新恢复原生历史；针对性单测通过。 |
| S06 | PASS | 浏览器实见实时字流及 WeKnora 工具事件。 |
| R01 | PASS | 原生引用顺序、chunk/knowledge ID 与正文保留，无按文件名推断。 |
| R02 | BLOCKED | 原 DOCX 同源鉴权下载并验 hash；图片放大未验。 |
| R03 | PASS | 缺失页码、分数等字段不补造。 |
| U01 | PASS | 原湾事通登录、主题、首页输入和推荐入口保留。 |
| U02 | PASS | 真实多轮追问、新问题和刷新历史。 |
| U03 | PASS | `/kb/admin/` 深链、静态资源与 API 浏览器验证。 |
| U04 | PASS | 管理页面/Logo 湾事通白标，许可证文件保留。 |
| U05 | BLOCKED | 迁移文档的原生上传/任务/chunks/前台问答已串通；管理浏览器新上传的完整闭环未跑。 |
| U06 | BLOCKED | 原生管理 UI 可设模型，私有归档 KB 的模型设置已保存；公开 KB 改后前台同步生效未专门测试。 |
| U07 | BLOCKED | 未启用的扩展功能未逐项验 UI 状态。 |
| D01 | PASS | 46 个当前源条目按 hash 归并为 45 个原生文档，13 个历史版本进私有归档；清单可追溯。 |
| D02 | PASS | 新问答只写原生会话与网关映射/反馈，不双写旧 chunk/向量。 |
| D03 | PASS | 公共范围固定当前 KB；历史归档 KB 不在公共范围。 |
| D04 | PASS | 真实用户浏览 66 条旧记录，旧历史只读，不能续聊或伪装原生引用。 |
| T01 | PASS | 真实“有帮助”反馈与 native message/Trace 对应。 |
| T02 | BLOCKED | 单条脱敏 Trace 下载约 134 KB 已验；批量下载未验。 |
| T03 | PASS | 导出区别 bridge/native，608 个原生事件、5 段引用；问题/答案正文未入脱敏文件。 |
| Q01 | BLOCKED | “是什么”已测且 OPC 错；“是啥”及其他定义题未完成。 |
| Q02 | FAIL | OPC 歧义样本错误；来源限定和大小写矩阵未完成。 |
| Q03 | FAIL | 预算题存在缺乏原文支持的签约前审批时点；其余时点/否定/长流程待测。 |
| Q04 | NOT_RUN | PDF 第 12 页、跨页表、同名列和图片样本未执行。 |
| Q05 | BLOCKED | 无证据题未编造额度，但引用了无关资料，需评估原生引用呈现。 |
| P01 | NOT_RUN | 共享模型同时服务内部生产，未做 1/2/4 并发负载。 |
| P02 | NOT_RUN | 未量测标题/摘要等后台模型调用与生产共享容量。 |
| P03 | NOT_RUN | 停止、用户断线只做代码检查，未测真实模型取消。 |
| O01 | BLOCKED | 七镜像离线包已生成、校验 checksum 并在 60 上 `docker load`；尚未在另一隔离主机做冷启动。 |
| O02 | BLOCKED | DB/文件/网关/Secret 分开备份并做格式/完整性检查；隔离恢复演练未执行。 |
| O03 | PASS | 旧 8289 版本/镜像保留，生产 18288/18290 容器仍运行、54 对应两个路径返回 HTTP 200；仅删除 11 个不再使用的早期候选 gateway/edge 镜像标签，8289 测试替换在本次授权内。 |

## 发布与回退边界

目前只把测试 8289 指向候选，不切正式入口。恢复旧测试版本的顺序见 [部署说明](README.md)；正式切换 18288 需另行完成本报告 BLOCKED/FAIL 项、备份恢复演练和明确批准。旧数据不自动反写：切回旧栈前先导出新候选期间产生的资料和会话。
