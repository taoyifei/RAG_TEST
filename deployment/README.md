# DOCX RAG 离线部署

本部署只创建或更新 `rag-app`、`rag-worker`、`rag-ocr`、`rag-qdrant` 及
对应 `rag-*` 网络。只有 `rag-app` 发布宿主机 8088；OCR 与 Qdrant
只在内部网络可达。服务器脚本不 build、不 pull、不安装软件、不访问外网，
也不删除 `/data/tyf/RAG` 下的 bind mount 数据。

## 服务器步骤

1. 分别校验 runtime/corpus 外层 SHA256，并用固定解包器验证内部 manifest。
2. 把 release 与 corpus 安装到 `/data/tyf/RAG` 固定布局；环境文件保存在
   `/data/tyf/RAG/shared/env/rag.env`。
3. 设置四个互不相同且至少 32 字符的令牌、经实测的模型端点、三个 bind
   mount 路径、`RAG_RELEASE_REVISION`、普通查询 `RAG_TRACE_MODE` 和 OCR
   使用的宿主 GPU ID。
4. 执行 `bash verify-offline.sh`。
5. 执行 `bash deploy.sh /data/tyf/RAG/shared/env/rag.env`；脚本先校验，
   再按白名单 `docker load`，最后运行
   `docker compose up -d --no-build --pull never`。
6. 检查 Compose、应用存活和容器内 OCR readiness：

   ```bash
   docker compose \
     --env-file /data/tyf/RAG/shared/env/rag.env \
     -f /data/tyf/RAG/current/compose.yaml ps
   curl -fsS http://127.0.0.1:8088/live
   docker exec rag-ocr python -c \
     "import urllib.request; print(urllib.request.urlopen(
     'http://127.0.0.1:8090/ready').read().decode())"
   ```
7. `/ready` 只有在检索参数冻结、活动索引与 manifest 一致且模型健康时返回
   200。通过管理 API 创建全量任务，由单个 `rag-worker` 串行执行。

应用把 Query Trace 单独写入 `/state/traces.sqlite3`；它随现有 state bind
mount 持久化，但不与任务或 manifest 表共库。管理员通过 `/debug/` 和
`/api/admin/traces*` 查询，FULL Debug 仅走 admin token。Trace Store 不加入
RAG readiness：普通查询捕获失败继续回答，显式 FULL Debug 则在执行前返回
503。详细内容边界与 72 小时/30 天保留策略见
`design/public/trace-observability.md`。

执行 `bash rollback.sh /data/tyf/RAG/shared/env/rag.env` 可切回部署前记录的
应用、OCR 和 Qdrant 镜像 ID；脚本保留 SQLite/Qdrant bind mount。索引数据
恢复仍以应用 manifest 中记录的 Qdrant snapshot 为准，不能通过删除数据目录
回滚。

资产下载、断网构建、双包、GPU 冒烟到回滚的完整命令见
`design/public/offline-build-and-server-deployment.md`；PaddleOCR 专用入口
保留在 `design/public/paddleocr-offline-deployment.md`。
