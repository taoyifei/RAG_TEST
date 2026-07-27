# DOCX RAG 离线部署

本部署只创建或更新 `rag-app`、`rag-worker`、`rag-ocr`、`rag-qdrant` 及
对应 `rag-*` 网络和卷。只有 `rag-app` 发布宿主机 8088；OCR 与 Qdrant
只在内部网络可达。服务器脚本不 build、不 pull、不安装软件、不访问外网，
也不删除卷。

## 服务器步骤

1. 校验外层 tar 的 SHA256，解包后进入目录。
2. 复制 `.env.example` 为 `.env`，设置四个互不相同且至少 32 字符的令牌、
   经实测的模型端点、DOCX 路径和 OCR 使用的宿主 GPU ID。
3. 执行 `bash verify-offline.sh`。
4. 执行 `bash deploy.sh ./.env`；脚本先校验，再 `docker load`，最后运行
   `docker compose up -d --no-build --pull never`。
5. 检查 Compose、应用存活和容器内 OCR readiness：

   ```bash
   docker compose --env-file .env -f compose.yaml ps
   curl -fsS http://127.0.0.1:8088/live
   docker exec rag-ocr python -c \
     "import urllib.request; print(urllib.request.urlopen(
     'http://127.0.0.1:8090/ready').read().decode())"
   ```
6. `/ready` 只有在检索参数冻结、活动索引与 manifest 一致且模型健康时返回
   200。通过管理 API 创建全量任务，由单个 `rag-worker` 串行执行。

执行 `bash rollback.sh ./.env` 可切回部署前记录的应用、OCR 和 Qdrant
镜像 ID；脚本保留 SQLite/Qdrant 卷。索引数据恢复仍以应用 manifest 中记录的
Qdrant snapshot 为准，不能通过删卷回滚。

OCR 从下载、断网构建、curl 到 GPU 验收的完整命令见
`design/public/paddleocr-offline-deployment.md`（源码树）或随包 README
引用的同版手册。
