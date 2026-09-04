# P11 V1 发布开工审计

## 审计基线

- 审计日期：`2026-09-04`。
- Start SHA：`e7d69f14e5ad293b091f6aef98c91f3a3f76e325`。
- 集成分支：`feature/universal-rag`。
- 阶段分支：`codex/p11-release`，已建立并推送到 `origin`。
- `main` 基线：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`，只读。
- `Industry` 基线：`5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`，只读。
- 开工时工作树干净，Start SHA 与 `origin/feature/universal-rag` 一致。

## P10.5 入口

远端 `origin/feature/universal-rag:docs/progress/phase-10-5.md` 明确记录
`P11_ENTRY_READY=true`。P10.5 集成 merge 为 Start SHA `e7d69f1`，其报告同时
限定所有既有 Provider 证据均来自 `httpx.MockTransport`，实际公网 Provider 与
Qdrant Server 调用数为零。

## 当前产品运行时

- 默认 `rag-app serve` 已调用 `create_product_lifespan_app`，是 P10.5 Product
  Runtime；旧运行时仅由显式 `rag-app legacy-serve` 启动。
- `deployment/product/compose.yaml` 只定义应用，但默认 `RAG_QDRANT_MODE=memory`，
  尚不是 P11 正式 Qdrant Server 合同。
- 根 `Dockerfile` 仍依赖预制离线 Wheelhouse，并复制旧前端与 Industry 资产；它不是
  P11 要求的前端构建、Python wheel 构建和最小 Python 运行时三阶段镜像。
- 根 `deployment/compose.yaml` 仍包含 OCR、GPU、Worker、四模型 Endpoint 等旧
  Industry 运行时要求，并显式调用 `legacy-serve`。

## Migration 与兼容性

- 已应用源码 Migration 范围为 `0001`—`0014`；P11 只能新增 `0015` 以后文件，
  不修改既有 Migration。
- 当前 Compatibility Manifest：应用 `0.1.0`、数据库 schema `14..14`、
  `document-ir-v4`、`canonical-chunk-v3`、`fts-v2`、Jina/Aliyun adapter `1`、
  Qdrant 范围 `>=1.18,<2`。
- 普通启动执行 schema 行为兼容检查；Git SHA 只用于追溯，不要求与运行目录完全相同。

## Provider Catalog 与 Secret Store

- Catalog 版本：`2026-09-04.1`。
- Jina：`jina-embeddings-v5-text-small`，支持 document/query embedding；
  `jina-reranker-v3.5`，支持 reranking。
- 阿里云百炼：`qwen3.7-text-embedding`，区域固定 `cn-beijing`，支持
  document/query embedding。
- 页面托管 API Key 已由 AES-256-GCM 保存到 SQLite，AAD 绑定 Credential、
  Provider、字段和密钥版本；公共读取只返回掩码摘要。环境托管模式只保存变量名。
- 当前进程未配置 `JINA_API_KEY`、`DASHSCOPE_API_KEY`、
  `ALIYUN_MODEL_STUDIO_WORKSPACE_ID` 或 `ALIYUN_MODEL_STUDIO_REGION`；本审计未读取或
  打印任何 Secret 值。

## Docker、Compose 与旧 Industry 文件

- P10.5 产品合同：`deployment/product/compose.yaml` 与其 `.env.example`。
- 旧 Industry/OCR 主体：根 `Dockerfile`、`deployment/compose.yaml`、
  `deployment/*.sh`、`deployment/config/`、`deployment/assets/` 与相关离线发布说明。
- P11 应让根默认路径只包含 `app` 与正式 `qdrant`，旧文件保留历史价值并移动或明确
  标记到 `legacy/industry-deployment`，不得继续出现在新用户默认路径。

## Live 用户授权与预算

首次 Live 调用前仍需用户明确授权，且需通过产品页面提供有效 Jina、阿里云百炼
Credential 与阿里 Workspace ID。拟议 Live 上限如下：

- 仅发送公开合成短文本和由公开合成文本生成的 DOCX；不发送企业文档、真实知识库、
  Secret、向量或 Provider 原始响应体。
- 服务：Jina Embeddings、Jina Reranker、阿里云百炼 Qwen3.7 Embedding。
- 最大公网 Provider 请求数：`30`。
- 最大估算输入 Token：`20,000`；任一 Provider 的可见实际 usage/配额更早触顶时立即
  停止。
- 操作：页面保存与连接测试、document/query embedding、完整候选 rerank、双槽建
  索引、正常查询、受控故障切换和恢复探测。

缺少授权或 Credential 不会被 Mock/Fake 代替。完成不依赖凭据的工作包后，将在 Live
Gate 创建 `docs/decisions/P11-live-provider-authorization.md` 并停止等待用户。

## CI 与仓库治理

- 当前分支没有 `.github/workflows/`，因此没有 P11 所需的 Python、前端、离线 E2E、
  容器、Qdrant、Secret scan 或 SBOM 工作流。
- GitHub 公共 API 在审计时只返回一条旧 Graph Update 成功 Run，head SHA 为
  `06b6325`；它不是当前 Start SHA 的 P11 CI 证据。
- GitHub 公共 API 返回 ruleset 数量 `0`。Branch Protection 详情接口在无认证请求下
  返回 `401`，因此保护规则当前不能声明就绪；最终单独报告
  `BRANCH_PROTECTION_READY=BLOCKED`，除非后续获得可验证证据。
- 禁止 force push、rebase 公共历史、自动合并原始 `main`。只有全部 P11 门为 true
  才允许 `--no-ff` 合并回 `feature/universal-rag`。

## 开工门禁

以下命令将在本文件建立后按顺序实际执行，任一失败立即停止：

```bash
python scripts/dev.py doctor
python scripts/dev.py check
python scripts/dev.py smoke
python scripts/dev.py product-check
python scripts/dev.py product-smoke
python scripts/dev.py web-e2e
git diff --check
```
