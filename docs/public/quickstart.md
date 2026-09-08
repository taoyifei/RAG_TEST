# V1 快速开始

## 前置

需要 Docker Engine 与 Compose V2。默认 Compose 只把应用发布到
`127.0.0.1:8088`，Qdrant 不发布宿主端口。若 8088 已占用，同时修改 `.env` 中的
`RAG_PORT` 和 `RAG_TRUSTED_ORIGINS`，两个值必须使用同一端口。

## 初始化与启动

```bash
cp .env.example .env
python scripts/release.py build
docker compose run --rm --no-deps app \
  init-secrets --directory /run/rag-secrets
docker compose up -d --no-build --pull never
docker compose ps
```

构建入口要求工作树干净且 HEAD 已提交。它从该提交的 Git 对象导出仓库外最小
白名单 context，正常使用 BuildKit，并把 source SHA、逐文件摘要、字节数和 Git
权限写入 `artifacts/release-context/`。Compose 不再提供工作树 build，避免把本地
证据、备份或其他未批准文件交给 context sender；源码变化后先提交，再重跑上述
构建命令。

`init-secrets` 不能覆盖已有文件。它创建 `master-key`、
`admin-bootstrap-token`、`qdrant-api-key` 和 `qdrant.yaml`，文件均为 0600。
`rag_secrets` 卷必须独立备份；主密钥丢失后，页面托管的模型密钥无法恢复。

读取首次登录 Token：

```bash
docker compose run --rm --no-deps --entrypoint sh app \
  -c 'cat /run/rag-secrets/admin-bootstrap-token'
```

命令会在当前终端显示 Secret，不要复制到工单、聊天、日志或 shell 参数。打开
`http://127.0.0.1:8088/` 登录后按顺序执行：

1. 在“模型服务”保存 Jina 连接；完成出网与预算授权后才测试。
2. 编辑原百炼连接，保留 Workspace ID 与 Credential，显式选择 `endpoint_mode`。`workspace_host` 使用北京控制台实际 API Host；`beijing_dashscope` 必须明确选择。先保存并做配置诊断，获准后才测试。
3. 创建项目与知识库，选择 Jina Primary、Qwen Standby 和 Jina Reranker。
4. 可先保存未激活草稿用于零调用预算估算；完成授权和连接验证后预览影响并激活。模型或维度变化会要求新索引 Revision。
5. 上传 DOC 或 DOCX，等待 Primary/Standby 覆盖率均为 100% 后问答。旧版 DOC
   只保留段落纯文本，不保证表格、图片、页眉页脚、批注或修订结构。
6. 在“接口访问”按最小 scope 创建外部 API Token；完整值只显示一次。

## 健康与停止

```bash
curl --fail http://127.0.0.1:8088/live
docker compose ps
docker compose stop
```

`/live` 只证明进程存活。模型连接、索引一致性、备份与真实问答必须分别验收。
`docker compose down` 保留命名卷，`docker compose down -v` 会删除数据、向量和
Secret，只能用于明确的一次性测试环境。
