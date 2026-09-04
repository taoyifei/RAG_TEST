# P11 V1 发布候选进度

本报告记录 2026-09-04 至 2026-09-05 的发布候选、原位部署与真实 Provider
结果。Jina 三项页面验证已成功；百炼 document 持续返回
`PROVIDER_REQUEST_INVALID`，已按任务书暂停，当前不得声明 P11 Ready。

## 身份与治理

- Start SHA：`e7d69f14e5ad293b091f6aef98c91f3a3f76e325`。
- 阶段分支：`codex/p11-release`；集成目标：`feature/universal-rag`。
- `main` 与 `Industry` 未修改；`MERGE_TO_MAIN_AUTHORIZED=false`。
- GitHub Ruleset API 返回空列表，`feature/universal-rag` Branch Protection 返回
  404。当前没有仓库设置权限授权，因此按任务书单独记为
  `BRANCH_PROTECTION_READY=BLOCKED`，本阶段没有自行修改仓库设置，也没有伪造
  保护状态。

## 已完成实现

- 根三阶段 Dockerfile；BuildKit frontend、Node 24 Alpine 与 Python 3.11 Trixie
  默认值均使用 `tag@digest`，Runtime 使用 `rag:rag`，无 Node、pip、setuptools、
  wheel，OCI revision 对齐候选 SHA。
- 根 Compose 只含 app/Qdrant，使用三个命名卷；Qdrant 不发布宿主端口，应用只绑定
  `127.0.0.1`。首次初始化创建 0600 Secret Bundle。
- Qdrant Server 1.18.3 与 client 1.18.0 已验证双 Named Vector、过滤、完整
  Inventory、七类损坏 Fail Closed、Snapshot、Restore、GC 与 restart。
- 页面托管 Credential、五项持久连接验证、知识库主备 Profile、双槽索引、查询、
  Reranker、预算和安全观测已经接入 Product Runtime。当前证据为 MockTransport，
  不能替代 Live。
- Word 上传同时支持签名一致的 `.docx` 与传统 OLE CFB `.doc`。DOCX 保留结构化
  `docx-ooxml-v4` 路径；DOC 使用受限 antiword 子进程并标记
  `word-document-v1`/`LEGACY_DOC_FLATTENED_TEXT`，原文件 SHA 继续作为身份。
- 统一备份含 SQLite、Blob、Qdrant Snapshot、Compatibility/Backup Manifest 和
  SHA；Secret 有意排除，恢复为非覆盖操作。
- Schema 15 覆盖空库与 P08.5/P09/P10/P10.5 升级，保留 checksum、失败回滚和
  FTS V1 Reindex 边界。
- Session、CSRF、CSP、TLS/Proxy、Origin、登录/查询/上传/Provider 限流、作用域
  API Token、吊销与错误脱敏已验证。
- GitHub Actions 包含 python/frontend/offline E2E/container/Qdrant/secret/SBOM
  七类作业。首次真实 Run `33862088499` 中 SBOM 通过，但 Python 与 Qdrant 作业
  因 `src/rag_app/core/models/console.py`、`lexical.py` 被根目录用途的
  `models/` 忽略规则意外排除而失败；本分支已将规则收紧为 `/models/`、补齐两个
  源文件，并新增运行时 Python 源必须受 Git 跟踪的架构门。第二次 Run
  `33863275211` 的 Qdrant、Frontend、Secret、SBOM、Container 五项通过；Python
  发现 19 个测试依赖本机忽略的冻结语料、Tokenizer 与 Python 3.10 环境，Web E2E
  发现服务解释器硬编码为 `.venv`。测试现改用公开合成语料与最小 Tokenizer，CI
  显式提供 Python 3.10，Web E2E 使用当前 Python。第三次真实 Run
  [`33864997116`](https://github.com/taoyifei/RAG_TEST/actions/runs/33864997116)
  在 SHA `abd50a128d479a3c8d5082fab41a5dd8994b9703` 上成功，Python、Frontend、
  Offline Product E2E、Container、Qdrant、Secret 与 SBOM 七类作业全部通过。后续
  文档提交触发的 Run `33865815809` 暴露 Web E2E 在点击吊销后未等待服务端响应，
  导致并发查询偶发早于吊销提交；验收现显式等待吊销响应及页面状态更新，再验证旧
  Token 返回 403。

## 实际门禁证据

- `python scripts/dev.py check`：`1459 passed, 79 deselected`；4 个已知警告。
- `python scripts/dev.py smoke`：72 passed。
- `python scripts/dev.py product-check`：59 passed。
- `python scripts/dev.py product-smoke`：6 passed。
- `python scripts/dev.py web-e2e`：3 passed、3 个按浏览器项目矩阵跳过。
- `tests/upgrade/test_p11_upgrade.py`：7 passed。
- 真实双 Qdrant、备份恢复、性能证据：3 passed；容器和卷按唯一名称清理。
- `python scripts/release.py build` 在 SHA `88a260263db425e645a7ef759106342fa4b9d95f`
  成功，manifest-list digest 为
  `sha256:a7c7b3f08969a68613e25fde2d5267ef053b2ecf77ac54bd58c4e5cb28e1830f`，
  镜像大小 120,065,123 bytes，运行用户为 `rag:rag`。
- `python scripts/release.py verify` 成功：pip-audit 无已知漏洞、npm audit 为 0、
  secret scan 1202 files、Trivy 179 条且可修复 High/Critical 为 0、SBOM 与
  license inventory 各 2914 components。
- `python scripts/release.py acceptance` 成功，统一入口重新执行完整离线门、升级、
  双 Qdrant、Snapshot/Restore、restart 与性能验收，报告
  `live_provider=NOT_RUN`。
- 原位部署前备份 `pre-88a2602.tar.gz` 校验通过，archive SHA-256 为
  `f849a43115029418a28ce4b3dc3f88d14ec333d05cdea5cd15e92b88f8d42ebb`，SQLite
  integrity 为 `ok`。只重建 app；Qdrant 容器 `1bc4088a449c` 与启动时间未变。
- 部署后 app 健康，OCI revision 为 `88a260263db425e645a7ef759106342fa4b9d95f`；
  页面保存的两条 Connection 与两个加密 Credential 均保留。
- 五项页面真实验证计划为 5 次 HTTP、231 估算输入 Token。第一项 Jina document
  embedding 实际尝试 1 次、发送 19 估算 Token，3250 ms 后以
  `PROVIDER_NETWORK_ERROR` 失败；其余四项、百炼、双槽 Live 与私有 DOC 出网均
  未运行。DNS、TCP、默认 CA 下 TLS 1.3 均通过，不能替代模型 HTTP 成功证据。
- 独立 Compose 演练：Secret 初始化、`/live`、管理员 Session、创建 Project、
  `down/up` 后 Session 与 Project 持久化通过；专用三卷已删除。
- pip-audit 曾完成并报告无已知漏洞；最终复核时 WSL Python 到 PyPI 的 TLS 握手
  超时，明确记录 `PIP_AUDIT_TRANSPORT=BLOCKED`。随后使用任务书允许的等价路径，
  经 OSV 官方 `querybatch` 实时检查 Runtime 锁文件全部 41 个精确版本，结果为 0 个
  受影响包。只有识别为传输故障时才允许切换；pip-audit 报告漏洞时仍立即失败。
- npm 官方 bulk advisory Endpoint 在 WSL、Windows PowerShell 和固定 Node 容器
  三条客户端路径均发生 TLS/socket 传输失败，明确记录
  `NPM_AUDIT_TRANSPORT=BLOCKED`。随后经 OSV 官方 `querybatch` 实时检查 npm V3
  lockfile 全部 410 个精确版本，结果为 0 个受影响包；未用失败结果冒充通过。
- Docker Hub 可变标签元数据解析连续发生 TLS handshake timeout；将成功构建已经
  解析的 BuildKit frontend、Node 与 Python 身份固定为精确 Digest 后，统一构建
  命令重新成功。普通 Compose 命令与按构建参数显式覆盖镜像的能力保持不变。
- Trivy 完整清单：54 个 Debian High/Critical，均无 FixedVersion；可修复
  High/Critical 阻断门为 0。完整风险保留在忽略跟踪的安全证据中。
- CycloneDX：镜像 2877 components，源码 1197 components；许可证清单由镜像
  SBOM 确定性生成。

## 单机观察值

环境为真实 loopback Qdrant Server + `httpx.MockTransport` Provider，20 次样本，
不是 SLA，也不是 Live 模型性能：

- 单块公开合成 DOCX 索引 0.958 s，1.044 chunk/s；
- Search p50 48.553 ms、p95 54.055 ms；
- Answer p50 49.019 ms、p95 53.775 ms；当前 answer 与 search 共用检索回答链；
- SQLite count p50 0.555 ms、p95 0.624 ms；
- Qdrant get_collection p50 3.765 ms、p95 5.690 ms；
- 首次 cache miss、第二次 cache hit；
- 峰值进程内存 168952 KiB；SHA `88a2602` 候选镜像观察值为 120065123 bytes，
  OCI revision 已核对一致。

## 验收状态

```text
PRODUCT_RUNTIME_READY=true
SIMPLE_COMPOSE_READY=true
QDRANT_SERVER_READY=true
MODEL_SERVICE_UI_READY=true
SECRET_AT_REST_READY=true
LIVE_JINA_EMBEDDING_READY=false
LIVE_JINA_RERANKER_READY=false
LIVE_QWEN_STANDBY_READY=false
AUTOMATIC_FAILOVER_READY=false
PRODUCT_E2E_READY=false
RESTART_PERSISTENCE_READY=false
BACKUP_RESTORE_READY=false
UPGRADE_READY=true
SECURITY_READY=true
DEPENDENCY_AUDIT_READY=true
CI_READY=true
BRANCH_PROTECTION_READY=BLOCKED
OBSERVABILITY_READY=true
REMOTE_PRODUCTION_PROFILE_READY=false
RELEASE_CANDIDATE_READY=false
MERGE_TO_MAIN_AUTHORIZED=false
P11_READY=false
```

Live Gate 见 `docs/decisions/P11-live-provider-authorization.md` 与
`docs/decisions/P11-live-jina-network-error.md`。凭据和授权均已具备，但首次 Jina
真实请求发生网络错误；其余 Live、完整产品 E2E、含真实 Provider 查询的恢复验收均
按失败即停规则未运行，不合并回 `feature/universal-rag`。


## 2026-09-05 Live 补充证据（取代上文旧 Provider 账本）

- `358f7560eb621f8b2a8736640fc36b558f51462b` 完成传输异常安全分类；完整
  离线门禁 `1464 passed, 79 deselected`。Jina document/query/reranking 均为
  `live_200`，维度、完整候选和 usage 通过。
- `c4e68dfa1ba61fef8ac590e81b663682bb0c1a2b` 完成百炼有界 JSON 白名单
  错误分类；定向测试 46 项和完整离线门禁
  `1471 passed, 79 deselected` 均通过。
- 部署前备份 `pre-c4e68df.tar.gz` 校验通过，SHA-256 为
  `22e9e3323ced776490815f3ab58e698d3e466455f61e97aacbe9d85f0a96f62a`。
  候选镜像为
  `sha256:0a67aa625aa05bf9b01a0ddc899cd4d19106fa9c59a03b05c36a9022376271ad`，
  120,069,319 bytes，OCI revision 与提交一致。
- 只重建 app 后容器 `05925fff4659` 健康；Qdrant 容器
  `1bc4088a449c` 未重启。数据库保持 2 个 Connection、2 个 Credential；
  重试前 5 条验证记录均保留。
- 新候选只执行 1 次百炼 document：270 ms 后为 HTTP 4xx、
  `PROVIDER_REQUEST_INVALID`，无 observed usage，随后立即停止。百炼 query、
  双槽 Live 与故障切换未运行。
- 当前总账为 6/25 次 Provider HTTP、157/1,000 估算输入 Token；Jina
  4 次、119/600，百炼 2 次、38/600；成功响应观察用量为 242 Token。指定私有
  DOC 未出网。
- Live Decision：
  [授权与预算](../decisions/P11-live-provider-authorization.md)、
  [Jina 瞬时故障](../decisions/P11-live-jina-network-error.md)、
  [百炼请求错误](../decisions/P11-live-aliyun-validation-error.md)。

## 2026-09-05 Workspace 零调用诊断与护栏部署

- 只做布尔/形状核验，没有输出 Workspace ID、API Key、哈希或企业正文。已保存的
  百炼 Region 为 `cn-beijing`，加密凭据与 Key 形状正常；Workspace ID 不以官方
  `llm-` 前缀开头，定位为当前真实 4xx 的配置根因。
- `224ac930be701cfd6d53ecede8501071cf9129da` 在 HTTP 前拒绝无效 Workspace ID；
  Provider 定向测试 47 项、原失败产品测试 5 项和完整离线门禁
  `1472 passed, 79 deselected` 均通过。
- 部署前备份 `pre-224ac93.tar.gz` 为 0 个 Collection、4 个文件、SQLite `ok`，
  SHA-256 为
  `2fcd5a2d4b9c9e9bc6506796122cce34d229a198298362347f2faae0a4ab0ed5`。
- 候选镜像 digest 为
  `sha256:f82968df74884c80008fb698aa3f55bea6128ffca68b93d22266ce53ceed731f`，
  120,069,668 bytes，OCI revision 与提交一致。只替换 app 后 `/live` 成功；Qdrant
  容器 `1bc4088a449c` 未重启，2 个 Connection、2 个 Credential 与历史验证均保留。
- 正式 Session/API 的生产护栏验收在 3 ms 内返回
  `PROVIDER_CONFIGURATION_INVALID` / `invalid_configuration`，Provider HTTP 为 0；
  第 7 条验证记录中的 19 Token 是未发送的本地估算。真实账本保持 6/25 次 HTTP、
  157/1,000 估算输入 Token；指定私有 DOC 未出网。
- Live Gate 现等待用户在页面保存控制台复制的、以 `llm-` 开头的正确 Workspace ID。
  保存前不再发起百炼请求；所有 P11 Live/发布 Ready 状态继续保持 `false`。

## 2026-09-05 统一 Acceptance 与候选刷新

- 首轮 `release.py acceptance` 按失败即停在桌面 Chromium：凭据轮换后未出现
  “无需重建索引”，升级与真实 Qdrant 子门禁未运行。失败快照证明 E2E 仍填写旧的
  `synthetic-workspace`，并且没有等待五次连接验证和凭据轮换请求完成。
- `94b5cfcbb0bd847c89c3d812b6b7d40c383be683` 改用合法合成 Workspace，逐次等待
  Provider 验证响应并断言 succeeded，凭据轮换也等待 2xx 响应。定向
  `web-e2e` 为 3 passed、3 skipped。
- 从头重跑统一 Acceptance 成功：check 1472 passed、79 deselected；smoke 72；
  product-check 72；product-smoke 6；web-e2e 3 passed、3 skipped；upgrade 7；
  隔离双 Qdrant、Snapshot/Restore、restart 和性能验收 3 passed。最终输出
  `OK release-acceptance live_provider=NOT_RUN`，隔离容器和命名卷均已清理。
- 当前候选镜像 digest 为
  `sha256:b096f14495660529f6c7317995dc3c10572ecdfeb5f83e4573423375bae1d17f`，
  120,068,787 bytes，用户 `rag:rag`，OCI revision 与 `94b5cfc` 一致。
- `release.py verify` 成功：pip-audit 无已知漏洞、npm audit 0、Secret scan
  1205 files、Trivy 179 条记录，其中 High/Critical 54 条均无 FixedVersion，
  可修复 High/Critical 为 0；SBOM 与许可证清单各 2914 components。
- 部署前备份 `pre-94b5cfc.tar.gz` 为 0 个 Collection、4 个文件、SQLite `ok`，
  SHA-256 为
  `e7201aab045a63768afd8d4902069fdd81d6a7414434031155445de0638d0f07`。
  只替换 app 后 `/live` 成功；Qdrant 容器 `1bc4088a449c` 未重启，2 个 Connection、
  2 个 Credential、7 条验证记录均保留。
- 本轮没有新增 Provider HTTP；真实总账仍为 6/25 次 HTTP、157/1,000 估算输入
  Token，指定私有 DOC 未出网。Live Gate 继续等待正确的 `llm-` Workspace ID。
