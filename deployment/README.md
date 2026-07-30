# DOCX RAG 离线部署

本部署只创建或更新 `rag-app`、`rag-worker`、`rag-ocr`、`rag-qdrant` 及
对应 `rag-*` 网络。只有 `rag-app` 发布宿主机 8088；OCR 与 Qdrant
只在内部网络可达。服务器脚本不 build、不 pull、不安装软件、不访问外网，
也不删除 `/data/tyf/RAG` 下的 bind mount 数据。

## 服务器步骤

1. 先用 `offline_bundle.py.sha256` 校验固定解包器，再分别校验
   runtime/corpus 外层 SHA256 和内部 manifest；解包器摘要失败时禁止执行。
2. 把 release 与 corpus 安装到 `/data/tyf/RAG` 固定布局；候选环境文件保存在
   `/data/tyf/RAG/shared/env/candidates`，活动环境文件固定为
   `/data/tyf/RAG/shared/env/rag.env`。`install.sh` 必须由 root 执行；
   发布前会把 release 全部固定为 `root:root`，目录与 Shell 为 0555，
   其他普通文件为 0444。复用既有 release 时只验证身份、文件集、owner 和
   mode，发现漂移会拒绝而不会静默修复。corpus 仍固定为
   `10001:10001`、目录 0700、文件 0400。
3. 设置 `release_id` 后创建候选目录。首次部署从 release 样例安装候选文件；
   升级则从当前活动文件复制到新 release 的候选文件。两种路径都只编辑候选
   文件：

   ```bash
   release_id='<40位小写Git SHA>'
   install -d -m 0700 /data/tyf/RAG/shared/env/candidates

   # 首次部署：
   install -m 0600 \
     "/data/tyf/RAG/releases/${release_id}/.env.example" \
     /data/tyf/RAG/shared/env/candidates/${release_id}.env

   # 升级：
   test ! -e /data/tyf/RAG/shared/env/candidates/${release_id}.env
   cp -- /data/tyf/RAG/shared/env/rag.env \
     /data/tyf/RAG/shared/env/candidates/${release_id}.env
   chmod 0600 /data/tyf/RAG/shared/env/candidates/${release_id}.env

   editor /data/tyf/RAG/shared/env/candidates/${release_id}.env
   ```

   设置四个互不相同且至少 32 字符的令牌、经实测的模型端点、三个 bind
   mount 路径、`RAG_RELEASE_REVISION`、普通查询 `RAG_TRACE_MODE` 和 OCR
   使用的宿主 GPU ID。首次部署与升级命令二选一，不要覆盖既有候选文件。
4. 执行 `bash verify-offline.sh`。
5. 只把候选文件传给 deploy；active rag.env 只能由 deploy.sh 成功后发布：

   ```bash
   bash "/data/tyf/RAG/releases/${release_id}/deploy.sh" \
     /data/tyf/RAG/shared/env/candidates/${release_id}.env
   ```

   脚本先校验，再按白名单 `docker load`，最后只启动 app、OCR 和 Qdrant。
   默认 Compose 路径不启动 worker。部署、回滚与失败补偿均按 deadline 等待：
   Qdrant 端口 health、容器内 Qdrant `/readyz`、app health 和 app
   `/live` 各最多 60 秒，OCR 最多 240 秒；OCR 的期限覆盖 90 秒 start
   period 与后续 12×10 秒健康重试窗口。`/readyz` 由 `rag-app` 容器内
   Python 使用容器环境中的 URL 和 API key 请求，不发布 Qdrant 宿主端口。

   脚本在 `docker load` 前严格分类部署状态：全部发布元数据、核心容器、
   worker 和 rollback state 都不存在才是 fresh；合法 active/current 配合
   完整核心容器是 installed；合法 active/current 配合全无核心、可选同旧 app
   image 的 worker 是 degraded。其余组合一律拒绝。fresh 不允许遗留 rollback
   state；installed/degraded 的旧 release 会在部署前和失败补偿前重新执行
   `verify-offline.sh`。
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
7. 当前 provisional 配置下 `/ready=503` 是正确结果，不得绕过 worker 的严格
   索引门禁。完成检索参数冻结和模型 revision 核验、生成对应 release 后，显式
   启动单索引 worker，再通过管理 API 创建任务：

   ```bash
   docker compose --profile index \
     --env-file /data/tyf/RAG/shared/env/rag.env \
     -f /data/tyf/RAG/current/compose.yaml \
     up -d --no-build --pull never rag-worker
   ```

`/ready` 只有在活动索引与 manifest 一致且全部模型健康时才返回 200。

升级或回滚前使用当前 release 内的可靠备份脚本；不要手工对运行中的 bind
mount 执行 `tar -czf`：

```bash
bash /data/tyf/RAG/current/backup.sh \
  "$(date -u +%Y%m%dT%H%M%SZ)" \
  /data/tyf/RAG/shared/env/rag.env
```

脚本只读取固定的 `data/state` 和 `data/qdrant`，验证归档与 SHA 后原子发布，
并恢复备份前实际运行的 app、worker、Qdrant 集合。

应用把 Query Trace 单独写入 `/state/traces.sqlite3`；它随现有 state bind
mount 持久化，但不与任务或 manifest 表共库。管理员通过 `/debug/` 和
`/api/admin/traces*` 查询，FULL Debug 仅走 admin token。Trace Store 不加入
RAG readiness：普通查询捕获失败继续回答，显式 FULL Debug 则在执行前返回
503。详细内容边界与 72 小时/30 天保留策略见
`design/public/trace-observability.md`。

执行 `bash rollback.sh /data/tyf/RAG/shared/env/rag.env` 可切回部署前记录的
应用、OCR 和 Qdrant 镜像 ID。脚本先重验旧 release、Compose、镜像 digest
与 OCI revision，再按回滚前实际状态决定是否恢复 worker；全部存活与镜像
检查通过后，才原子持久化共享 env 和 `current`，提交失败会恢复原元数据。
SQLite/Qdrant bind mount 不会被删除。索引数据恢复仍以应用 manifest 中记录的
Qdrant snapshot 为准，不能通过删除数据目录回滚。

资产下载、断网构建、双包、GPU 冒烟到回滚的完整命令见
`design/public/offline-build-and-server-deployment.md`；PaddleOCR 专用入口
保留在 `design/public/paddleocr-offline-deployment.md`。
