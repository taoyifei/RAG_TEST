# P11 V1 发布候选进度

本报告记录 2026-09-04 的凭据前状态。最终 Live、CI 与集成 SHA 将在各门真实通过后
更新；当前不得据此声明 P11 Ready。

## 身份与治理

- Start SHA：`e7d69f14e5ad293b091f6aef98c91f3a3f76e325`。
- 阶段分支：`codex/p11-release`；集成目标：`feature/universal-rag`。
- `main` 与 `Industry` 未修改；`MERGE_TO_MAIN_AUTHORIZED=false`。
- GitHub Ruleset API 返回空列表，`feature/universal-rag` Branch Protection 返回
  404。因此 `BRANCH_PROTECTION_READY=false`，本阶段没有自行修改仓库设置。

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
  显式提供 Python 3.10，Web E2E 使用当前 Python。真实成功 Run 尚待再次推送确认。

## 实际门禁证据

- `python scripts/dev.py check`：`1396 passed, 79 deselected`；4 个已知警告。
- `python scripts/dev.py smoke`：71 passed。
- `python scripts/dev.py product-check`：33 passed。
- `python scripts/dev.py product-smoke`：6 passed。
- `python scripts/dev.py web-e2e`：3 passed、3 个按浏览器项目矩阵跳过。
- `tests/upgrade/test_p11_upgrade.py`：7 passed。
- 真实双 Qdrant、备份恢复、性能证据：3 passed；容器和卷按唯一名称清理。
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

- 单块公开合成 DOCX 索引 0.969 s，1.032 chunk/s；
- Search p50 48.282 ms、p95 51.642 ms；
- Answer p50 49.100 ms、p95 54.429 ms；当前 answer 与 search 共用检索回答链；
- SQLite count p50 0.558 ms、p95 0.621 ms；
- Qdrant get_collection p50 3.599 ms、p95 5.559 ms；
- 首次 cache miss、第二次 cache hit；
- 峰值进程内存 169832 KiB；候选镜像 119811235 bytes。

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
PRODUCT_E2E_READY=true
RESTART_PERSISTENCE_READY=true
BACKUP_RESTORE_READY=true
UPGRADE_READY=true
SECURITY_READY=true
DEPENDENCY_AUDIT_READY=true
CI_READY=false
BRANCH_PROTECTION_READY=false
OBSERVABILITY_READY=true
REMOTE_PRODUCTION_PROFILE_READY=false
RELEASE_CANDIDATE_READY=false
MERGE_TO_MAIN_AUTHORIZED=false
P11_READY=false
```

Live Gate 见 `docs/decisions/P11-live-provider-authorization.md`。在用户配置页面凭据并
明确授权预算之前停止真实 Provider 验收；不合并回 `feature/universal-rag`。
