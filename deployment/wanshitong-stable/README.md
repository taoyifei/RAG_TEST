# 湾事通 × WeKnora 隔离候选部署

此目录定义并部署测试候选栈，不执行生产入口切换。`compose.engine.yaml` 是 WeKnora 原生服务；叠加 `compose.wrapper.yaml` 后，由湾事通网关和同源静态入口提供 React 用户页、白标 Vue 管理页。湾事通只处理已有身份、会话映射、权限、SSE 传输和引用访问，问答、解析、分块、检索、重排、生成与模型配置使用同一套 WeKnora。原生 `app`、Docreader、PostgreSQL、Redis 均不发布宿主端口，浏览器无法直连原生 `/api/v1`。

2026-09-26 的现场测试入口是 `http://10.242.180.54:8289/kb/`，由 54 的原测试转发进入 60 上独立候选栈。原 8289 应用容器已停止但保留（`14c0f9a42c14`），原镜像 `sha256:ca38110829dd5a1e65589e9571744a1c7342b3b2fc95e94004ba9f7aa9168d06`、数据及切换前 SQLite 快照均保留。生产 `54:18288`、`60:18288`、`60:18290` 及其镜像不在本目录的操作范围内。具体结果和未完成项见 [验收记录](ACCEPTANCE-20260926.md)。

源码分支 `wanshitong-stable` 在 `/home/jerry/work/RAG-wanshitong-stable` 独立 worktree 中。VS Code 应直接打开此目录；Git 不允许同一分支同时检出到原目录 `/home/jerry/work/RAG`。

## 固定版本与隔离

基于 `vendor/weknora` 的 v0.8.2（`3e8b0bfc80b845b2d4b2ed683994748741450a97`）。原版 Compose 的固定 `container_name`、宿主端口和可选服务已去掉。候选服务不设置 `container_name`；网络和卷由独立 `WST_PROJECT_NAME` 前缀命名。PostgreSQL 只使用 `native-postgres` 新卷与 `wst_weknora_candidate` 数据库，文件、Redis、Docreader 临时目录和网关 SQLite 也各用候选卷。不得复用旧 8289 或 18288 的卷、网络、配置目录、容器名和数据库。

下列镜像 digest 已由现场原版 B 的 v0.8.2 镜像核验，且在原生 Compose 中固定使用：

| 组件 | 固定引用 |
|---|---|
| App | `wechatopenai/weknora-app@sha256:bc8a534799fcb54045100daf4bc5db86270b38ae45c5f761c410c50b4dcd6cdc` |
| Docreader | `wechatopenai/weknora-docreader@sha256:3c36e7e738ba515315523876d8041f9bb02809145a4e3b331e2550e4bff98b7a` |
| ParadeDB | `paradedb/paradedb@sha256:58bf87d2a6f1e72f56590b0f0ae1467e82c3f99ca203dde646672c8ac13148b2` |
| Redis | `redis@sha256:c9d92d840fd011c908f040592857c724ae6d877f2aba5c40ad963276507386b2` |

原版 B 的 Vue UI 镜像为 `wechatopenai/weknora-ui@sha256:83cfd9272e1219b5b5729f89d9c3f8d15a16e1b8cc17cacf638153cc0417290f`，仅用来对照原版。候选管理页必须用当前分支的白标 Vue 源码重建，不运行 B 的 UI 镜像。

Compose 对所有运行镜像使用 `pull_policy: never`，没有 `build:`，不会在 60 主机隐式拉取或构建。运行前须将固定镜像加载到 60；网关和静态入口各构建候选专用镜像并填写其不可变镜像引用。构建阶段也须使用已预加载的固定基础镜像、离线依赖和 `--network=none --pull=false`。上游 `Dockerfile.app`/`Dockerfile.docreader` 含 Go/Rust、Playwright 等在线下载步骤，不能直接在隔离运行阶段执行。

## 配置与 Secret

将 `.env.example` 复制为本目录 `.env`，只填写非密钥参数。`WST_SECRET_DIR` 必须是 60 主机上独立的绝对路径，不能指向仓库或旧栈目录；不要提交密钥文件。至少准备只读 Secret 文件：`db_password`、`redis_password`、`jwt_secret`、`system_aes_key`（32 字节 ASCII）、`system_signing_key`。WeKnora 固定版只读取相应环境变量，因此 `weknora-secrets-entrypoint.sh` 在容器内读文件并交给原版入口脚本，密钥不出现在 Compose 配置或仓库中。`system_aes_key` 丢失会使原生数据库内加密的模型等凭据无法恢复，应与数据库备份配套保存。

启用包装层时还需 `sso_client_secret`、`gateway_master_key`、`gateway_admin_bootstrap_token`、`workspace_api_key`、`external_signing_key`、`weknora_admin_password`、`legacy_master_key`。网关代码要求密钥文件为非 symlink、仅所有者可读；候选镜像以 UID 10001 运行，现场这些文件由 UID 10001 所有、权限 `0600`。Compose 使用逐文件只读 bind mount 且禁止自动创建缺失源路径。`WST_WEKNORA_TENANT_ID` 是原生真实正整数，`WST_PUBLIC_KB_IDS` 是已发布的非空原生 KB 列表；空范围拒绝启动。旧版记录只读访问独立的切换前 SQLite 快照和旧主密钥，不继续旧会话，也不调用旧算法。

`WST_SSO_ENTRIES` 来自现场登记的 8289 入口；候选 Origin、回调和 `RAG_TRUSTED_ORIGINS` 必须一致。现场用真实 RDMS Ticket 与验证码完成了公共用户登录，没有伪造 Ticket。60 不能直连 RDMS，故使用 60 上专用 `wst-weknora-sso-relay.service` 经 54 的受限 SSH 端口转发访问验证接口，服务已启用并可自动重连。专用私钥只留在 60 的候选 Secret 目录；54 的对应公钥仅允许来自 60 且只准转发 RDMS 指定端口。SSH 服务、Docker 候选网络或 RDMS 地址变更时，必须先检查中继状态和登录，再继续发布。workspace API Key、外部主体签名 Key 与原生管理账号均为候选独立配置；管理浏览器只持有湾事通管理 Cookie，不持有原生 JWT。

原生模型、embedding、reranker、OCR 的连接信息由 WeKnora 管理配置负责。`WST_ALLOWED_MODEL_HOSTS` 填经现场核实的内网主机白名单。`model-egress` 网络只给原生 app 访问内网模型；Docker 网络本身不保证禁止公网，60 主机须用出站策略限制它。Docreader 只在内部网络；默认不启用 ODL hybrid、沙箱、联网、MCP、图谱或 Langfuse。现场 Qwen3-8B-AWQ 物理上下文为 8192；原生候选配置将 `max_completion_tokens/max_output_tokens` 设为 2048、`rerank_top_k` 设为 3 后，原先触发 400 的真实问题可完成。这个额度只配置 WeKnora 原生模型请求，湾事通网关和前端不裁剪问题或答案；内容质量仍须逐题核查。`CONCURRENCY_POOL_SIZE=4` 不是共享模型全局四并发保证，仍须测原生额外模型调用和旧服务共享负载。

原生应用的 `AUTO_MIGRATE=true` 仅作用于候选新卷。升级前备份该卷；旧镜像不可直接连接升级后的数据库。`DISABLE_REGISTRATION=true`，原生候选管理员已在不对外暴露原生 API 的条件下建立。白标管理端通过网关受控代理使用原生知识库、文档、任务、分块和模型接口；管理端无第二次腾讯登录。

管理员在 `http://10.242.180.54:8289/kb/admin/ops/` 输入候选专用令牌，进入湾事通运营页；“知识库与文件”“模型设置”“处理任务”进入同一管理会话下的原生后台。令牌存放在 60 主机的候选 Secret 目录，可在 60 上执行 `docker exec wst_weknora_candidate_20260925-gateway-1 cat /run/wst-secrets/gateway_admin_bootstrap_token` 读取。不要把令牌写入仓库或前端构建文件。

## 构建包装层镜像

`Dockerfile.gateway` 以预加载的 Python 3.11 运行镜像为基础，该镜像必须已有本项目锁定的 FastAPI、HTTPX、Uvicorn 等依赖。它只复制 `src/` 并运行 `wanshitong_gateway.app:create_app --factory`，不执行旧内核入口。建议使用已验证的候选 Python 基础镜像 digest 或镜像 ID；先执行 `docker image inspect` 确认本机有该镜像。示例命令从仓库根目录执行：

```sh
docker build --network=none --pull=false \
  --build-arg WST_GATEWAY_RUNTIME_IMAGE="$WST_GATEWAY_RUNTIME_IMAGE" \
  -f deployment/wanshitong-stable/Dockerfile.gateway \
  -t wst-gateway-candidate:v1 .
```

`Dockerfile.edge` 只复制预构建静态产物。React 的 `frontend/dist/` 应以现有 `base: "./"` 构建；Vue 使用当前分支白标源码并以 `/kb/admin/` 为 Vite base 构建。先在受控构建机完成 `npm ci`/构建所需离线依赖准备，再将两份 `dist` 放入本目录的 `build-context/react/` 与 `build-context/vue/`（此目录被忽略）。检查 `index.html` 的资源路径后，在本目录离线构建：

```sh
docker build --network=none --pull=false -f Dockerfile.edge \
  -t wst-edge-candidate:v1 .
```

`Dockerfile.edge` 的 Nginx 基础镜像 digest 与固定版上游 `frontend/Dockerfile` 一致。不得把原版 B 的未白标 UI 镜像直接作为候选界面。对两个新镜像记录最终 image ID/RepoDigest、构建输入 hash，并在 `.env` 填 `WST_GATEWAY_IMAGE`、`WST_EDGE_IMAGE` 的不可变引用；只有本地浮动 tag 时不得进入发布验收。

## 只读配置校验与启动边界

在 60 主机先盘点 `docker ps`、镜像引用、卷、网络、端口、真实回调与模型容量。现场已在保留旧版后切换测试 8289；`WST_BIND_IP=127.0.0.1`，经 54 的既有测试转发对外可见，不改 18288 路由。

```sh
docker compose --env-file .env -f compose.engine.yaml config --quiet
docker compose --env-file .env -f compose.engine.yaml \
  -f compose.wrapper.yaml config --quiet
```

以上只是 Compose 结构校验，不代表镜像/Secret 已存在或服务可用。现场测试还应分别检查 `docker compose ps`、真实登录、提问和管理页面。不得运行 `down -v`、全局 prune，或将旧容器/镜像当作候选资源清理。本目录不包含生产 18288 切换命令。

## 离线镜像与数据备份

现场离线基础镜像包位于候选根目录的 `backup/images/wst-candidate-20260926.tar.gz`，包含 r6 时的七个镜像：WeKnora app、Docreader、ParadeDB、Redis、Python rerank adapter 基础镜像、湾事通 gateway 和 edge。当前 r7/r8 包装层另存于 `backup/images/wst-wrapper-r7-r8-20260926.tar.gz`；恢复当前候选须按顺序加载两个包。核对随包 SHA-256 后运行 `gzip -dc ... | docker load`；再用 `docker image inspect` 核对当前七个 Image ID。`docker save/load` 不保证保留 RepoDigest，离线恢复时把核验后的 `sha256:<Image ID>` 写入 `.env` 的对应镜像变量，并在原两份 Compose 文件之后叠加 `compose.offline-images.yaml`。只在候选项目和候选卷执行，不接旧数据库。

候选备份目录与镜像包分开保存 PostgreSQL 自定义格式 dump、原生文件卷快照、网关 SQLite 快照和 Secret 文件归档。数据库和 `system_aes_key` 必须同批保存；Secret 归档只在 60 的受限目录，不进 Git 或镜像包。恢复时先隔离停掉候选入口，再恢复配套的镜像、原生 DB、文件卷、网关 SQLite 与 Secret。旧 WeKnora 镜像不能直接连接自动迁移后的新 DB。新候选期间新增的会话或资料需先导出；回退到旧 8289 不自动反写这些数据。

测试入口回退顺序：先确认原容器 `14c0f9a42c14` 及原镜像存在；用候选 Compose 文件仅停止 `edge` 和 `gateway`，释放 60 的 `127.0.0.1:8289`；再启动原容器并通过 54:8289 检查旧版登录和历史。不要停止或修改 18288/18290。反向切回候选时先停止原容器，确认端口空闲，再启动候选 `gateway`、`edge` 并检查真实 SSO、问答和管理端。此处是运行手册；是否执行切换以实际测试/发布安排为准。
