# Word 文档 RAG

这是一个可追溯、可增量更新、可恢复的 DOC/DOCX 检索增强生成产品。默认
`rag-app serve` 提供中文管理控制台和稳定 API；V1 使用 SQLite 保存产品状态，
使用正式 Qdrant Server 保存向量。Jina 与阿里云百炼凭据由管理员在页面配置，
不会写入镜像或浏览器存储。

## 五分钟容器路径

首次拉取基础镜像和构建时间不计入五分钟操作路径。默认只启动 `app` 与
`qdrant`，应用端口只绑定宿主 loopback：

```bash
cp .env.example .env
docker compose build app
docker compose run --rm --no-deps app \
  init-secrets --directory /run/rag-secrets
docker compose up -d
docker compose ps
```

初始化命令排他创建 0600 主密钥、Bootstrap Token、Qdrant API Key 与配置。
先把 `rag_secrets` 卷和主密钥单独备份，再用下面的命令在当前终端读取一次
Bootstrap Token：

```bash
docker compose run --rm --no-deps --entrypoint sh app \
  -c 'cat /run/rag-secrets/admin-bootstrap-token'
```

打开 `http://127.0.0.1:8088/`，输入 Bootstrap Token。随后在“模型服务”依次
保存 Jina 与阿里云百炼连接。百炼须显式选择端点模式，业务空间模式使用控制台实际 API Host。完成出网与预算授权后测试连接，创建项目和知识库、激活主备检索方案，上传
DOC 或 DOCX 后即可问答。DOCX 保留结构化解析；旧版 DOC 以受限纯文本模式解析，
并明确记录结构降级。发送到远程 Provider 的只有管理员明确授权的查询、文档切片
和重排候选；页面会显示操作、Token 与切换用量。没有凭据时仍可完成本地 Exact/FTS
检索，但不得把它称为 Live Ready。

完整步骤见 `docs/public/quickstart.md`，部署与 TLS 见
`docs/public/deployment.md`，数据出网边界见
`docs/public/data-egress-and-cost.md`。

## 发布验收

Python 3.11 虚拟环境安装 `requirements.lock` 后，根目录只需这些主入口：

```bash
python scripts/dev.py check
python scripts/dev.py smoke
python scripts/dev.py product-check
python scripts/dev.py web-e2e
python scripts/release.py build
python scripts/release.py verify
python scripts/release.py acceptance
```

默认验证离线且无 Key。真实验收支持既有本地有界续跑与受保护的
`P11 Live Provider` Workflow；两者都要求明确授权、持久 campaign、累计预算
和公开合成语料边界。Workflow 的授权短语与 Environment 审批不代替预算批准。
先执行零调用配置诊断，前置条件具备后才运行获准的 Live 步骤：

```bash
python scripts/release.py acceptance --resume --steps config_check \
  --container <目标App> --config <本地非秘密配置> --exit-scope selected
python scripts/release.py budget-plan --config <本地非秘密配置> \
  --container <目标App> --budget-history <已核对的累计账> \
  --plan-output release/p11-budget-plan.json
```

预算配置复用 `source_profile_revision_id`，允许合法未激活草稿；无方案时仅输出
`actual_profile_bound=false` 的默认假设，不能直接批准。审批前与执行前重新生成并
核对方案身份，变化则计划失效、历史用量保留。所选步骤退出码为 0 不等于 P11_READY。
完整步骤与当前未决条件见 [P11 最终验收](docs/progress/p11-final-acceptance.md)
和 [当前验收](release/p11-repair-acceptance.md)。

## 主要组成

- `src/rag_app/`：安全 DOC/DOCX 解析、产品状态、检索、引用、备份与 API。
- `frontend/`：React/TypeScript 中文控制台与 Playwright 测试。
- `compose.yaml` 与 `Dockerfile`：V1 默认的简单容器路径。
- `evaluation/`：独立评测 schema、证据校验和指标计算。
- `deployment/`：只为迁移保留的旧 Industry/OCR 部署资产。

## 备份与数据生命周期

停止写流量和索引任务后，使用已有 Product 命令创建并校验备份：

```bash
docker compose exec app rag-app backup create --data-dir /data \
  --output /data/backups/p11-backup.tar.gz \
  --compatibility-manifest /app/compatibility-manifest.json
docker compose exec app rag-app backup verify \
  --archive /data/backups/p11-backup.tar.gz
```

备份不包含主密钥等 Secret，需独立保存。恢复使用新的空数据目录与独立 Qdrant，
具体参数见 [备份与恢复](docs/public/backup-restore.md)。当前 Product 没有独立
GC CLI；旧 `index-gc` 面向 Legacy 数据库，不能用于产品数据卷。

登录与权限见 [产品安全](docs/public/security.md)；DOCX 支持边界见
[结构支持矩阵](docs/public/docx-support-matrix.md)。旧 DOC 使用受限纯文本解析，
不保证原二进制文档的表格、图片、页眉页脚、批注或修订结构。

旧 `RAG_RELEASE_REVISION`、tokenizer/pipeline/retrieval JSON、debug/admin token、
GC 与七文件部署说明完整保留于 [Legacy 运行指引](docs/public/legacy-runtime.md)，
迁移见 [Industry 迁移](docs/migration/from-industry.md)。
