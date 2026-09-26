# 湾事通 8289 候选管理能力矩阵

基线：`wanshitong-stable` 的固定 WeKnora v0.8.2（上游 `3e8b0bfc80b845b2d4b2ed683994748741450a97`）。本表对照原生路由、湾事通 `NativeAdminClient._allowed_admin_path`、候选 Compose/边缘路由及 [8289 验收记录](ACCEPTANCE-20260926.md)。此轮只做源码核对与 Vue 入口约束；表中的现场证据引自既有验收记录，未把源码存在或 HTTP 200 当作完整功能验收。

状态含义：**支持**表示指定范围已有候选现场证据；**主动关闭**表示当前网关或边缘明确不发布；**缺依赖**表示虽有原生实现但候选未提供运行依赖；**未验证**表示入口或路由可达，仍缺实际功能闭环。一个功能的配置页能打开，并不证明外部回调、模型效果或后台任务生效。普通用户入口始终通过湾事通网关，不持有原生管理员 JWT。

| 功能 | 前端入口 | 候选网关路由 | 固定版原生 API | 当前依赖 | 验证证据 | 状态 |
|---|---|---|---|---|---|---|
| 普通问答与引用 | `/kb/` 湾事通 | `/api/public/chat`、历史、引用资源 | `/knowledge-chat`、`/sessions`、`/messages`、`/knowledge` | RDMS、固定公开 KB、LLM/Embedding/Rerank | 8289 真实登录、增量正文、引用及多轮见验收记录 | **支持**；业务正确性另有 P0 |
| 运营统计、反馈、单条 Trace | `/kb/admin/ops/` | `/api/admin/ops/*` | 反馈与消息原生 ID 的映射 | 网关 SQLite | 真实提问者、问题榜、单条 Trace 可见 | **支持**；批量导出和复核写入仍未验证 |
| 知识库、文档、解析任务、分块 | `/kb/admin/platform/knowledge-bases` | `/api/engine-admin/api/v1/knowledge-bases`、`knowledge`、`chunks` | 同名前缀，另有 `/files` | Docreader、ParadeDB、Redis、本地文件卷 | 迁移 45 份当前原件与 13 份历史原件；私有 TXT 上传及分块现场通过 | **支持**（所述样本）；新 PDF/DOCX 完整闭环未验证 |
| FAQ、Wiki、文件夹、标签、删除/重解析 | 知识库详情 | `faq`、`knowledgebase`、`knowledge`、`chunks` 前缀允许 | `/knowledge-bases/:id/faq`、`/knowledgebase/:kb_id/wiki`、`/knowledge/*` | 原生文档服务 | 固定版路由及代理白名单可确认；未逐项现场操作 | **未验证** |
| 知识库数据源同步 | 知识库数据源设置 | `datasource` 前缀允许 | 原生数据源路由 | 外部源地址、凭据、同步任务 | 路由允许；未建立外部数据源与回调 | **未验证** |
| 模型列表及原生配置 | 设置→模型 | `models` 前缀允许 | `/models` | 候选共享模型端点 | 原生管理页实见 LLM/Embedding/Rerank | **支持**（列表/配置可见）；变更公开 KB 后生效未验证 |
| 向量存储、文件存储配置 | 设置→向量存储/存储 | `vector-stores`、`storage-backends`、`system` 前缀允许 | 同名前缀 | 当前 ParadeDB、本地文件卷 | 运行栈存在；切换提供者和重建索引未实测 | **未验证**；外部存储未配置 |
| 管理后台原生聊天/Agent 流 | 原生管理聊天 | `knowledge-chat`、`agent-chat` 前缀允许 | `/knowledge-chat`、`/agent-chat` | 同一模型与会话服务 | 普通问答流通过；管理代理流式为本批次另行修复 | **未验证**（管理流） |
| 智能体配置页 | 原生侧栏 Agent | `agents` 前缀允许；编辑器还预取被拒绝的 `mcp-services`、`sandbox-configs` | `/agents` 及 MCP/沙箱子依赖 | 当前未开放 MCP/沙箱 | 仅静态调用链证据；候选 Vue 暂隐藏 Agent 入口 | **主动关闭**（候选管理入口） |
| 对外 API/嵌入/IM 渠道 | 设置→集成 | 部分 `embed-channels`、`im-channels`、`tenants` 前缀允许 | `/embed/*`、`/im/callback/*` 等 | 对外入口、回调网络、独立鉴权方案 | 边缘只把 `/api/` 交给湾事通网关，未开放原生回调；候选 Vue 隐藏入口 | **主动关闭**（发布运行）；配置路由存在 |
| MCP 服务与 MCP Endpoint | 工具箱、集成 | `/mcp-services`、`/mcp-endpoints` 不在白名单 | 固定版 `routes_infra.go`、`routes_mcp_endpoint.go` 已注册 | 外部服务/令牌及受控出站 | 原生路由有；候选代理拒绝，Vue 隐藏入口 | **主动关闭** |
| 原生评测 | 当前无独立 Vue 评测页 | `/evaluation` 不在白名单 | `POST/GET /evaluation` | 评测样本、模型配额 | 仅固定版路由；候选未开放 | **主动关闭** |
| 技能、沙箱、Artifact | 工具箱/设置/Artifact | `/skills`、`/sandbox-configs` 不在白名单；`system/sandbox-check` 单路由不能替代运行能力 | `/skills`、`/sandbox-configs`、相关会话路由 | `WEKNORA_SANDBOX_DOCKER_ENABLED=false`；无独立沙箱服务 | 配置及代理均关闭；候选 Vue 隐藏入口 | **缺依赖**，并主动关闭 |
| 长期记忆 | 设置→个人/工作空间记忆 | `/memory` 不在白名单；`tenants/kv` 的设置路径可达不代表记忆 API 可用 | `/memory/settings`、`/memory/items` 等 | 原生记忆服务和对应权限 | 原生路由有；代理拒绝；Vue 隐藏入口 | **主动关闭** |
| 本机浏览器连接 | 工具箱→浏览器连接 | `/local-browser`、`/me/browser` 不在白名单 | `/local-browser/*`、`/me/browser/*` | `BROWSERSKILL_BINARY` 为空，未配置客户端连接 | 入口与服务依赖均未就绪；Vue 隐藏入口 | **缺依赖**，并主动关闭 |
| 平台系统管理员设置 | 设置→系统管理 | `/system/admin/*` 明确拒绝；`/system/host-project-dir` 也拒绝 | 原生系统管理、队列、审计、平台 Key | 独立平台级授权 | 网关白名单可确认；Vue 隐藏快捷入口及深链设置节 | **主动关闭** |
| Ollama/云模型服务 | 设置→运行时/云模型 | Ollama 下载路径拒绝；其他模型/系统配置可达 | 原生模型及 Ollama 接口 | `OLLAMA_BASE_URL=127.0.0.1`、`OLLAMA_OPTIONAL=true`，候选无 Ollama 服务；无云模型配置 | 无候选运行证据；Vue 隐藏入口 | **缺依赖** |
| 外部 Web 搜索 | 设置→Web 搜索 | `web-search-providers` 前缀允许 | 原生 Web Search Provider | 候选出站仅允许指定内网模型主机，未配搜索 Provider | 仅配置 API 可达；候选 Vue 隐藏入口 | **缺依赖** |
| 原生内部 Trace/Langfuse | 运营 Trace 仅展示网关与 SSE 事件 | 当前 `trace_export.native_trace=NOT_CONFIGURED` | 原生内部诊断需另行接入 | `LANGFUSE_ENABLED=false` | 无检索/重排/实际送模全链路 Trace | **缺依赖**；网关 Trace 不能冒充原生内部 Trace |
| 跨空间组织/成员 | 原生侧栏组织、设置→成员 | `organizations`、`tenants` 路由部分允许 | 组织与成员原生 API | 候选禁止跨租户访问与自助创建；服务账号只绑定固定候选空间 | 未做双租户验收；候选 Vue 隐藏管理入口 | **主动关闭** |

## 候选 Vue 的入口规则

`GET /api/v1/system/capabilities` 仅报告原生注册路由，不能反映湾事通代理、边缘回调和运行依赖。本轮在 `VITE_WANSHITONG_GATEWAY_AUTH=true` 的构建中增加候选能力遮罩：MCP、沙箱、技能、浏览器、记忆、系统管理、外部渠道、Ollama、Web 搜索、组织和依赖被拒绝子路由的 Agent 管理入口均关闭；模型、知识库、FAQ/Wiki、解析与本地存储入口保留。原生模式仍使用原有能力探测行为，网关仍执行服务器白名单。直接访问工具箱路由也会被能力守卫退回知识库。

此遮罩是当前固定候选配置的前端说明，不能作为安全控制。若日后放行某一能力，应先核对原生 API、网关白名单、边缘路由、依赖与权限，再更新前端能力表并做实际闭环验证。较稳妥的后续改进是由网关提供服务端计算的有效能力接口，避免前端静态表与白名单漂移；本批次不为此扩大管理代理范围。
