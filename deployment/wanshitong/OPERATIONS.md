# 湾事通内网 Demo 运维

以下命令均从 `/data/tyf/wanshitong/deployment` 执行，并显式指定目标主机的
私有 `.env`。这些操作不清理镜像、卷或其他项目容器。

## 状态与日志

```bash
sudo docker compose --env-file .env -f compose.yaml ps
sudo docker compose --env-file .env -f compose.yaml logs --tail=200 wanshitong-app
sudo docker compose --env-file .env -f compose.yaml logs --tail=200 wanshitong-qdrant
curl -fsS http://127.0.0.1:8288/live
curl -fsS http://127.0.0.1:8288/ready
```

实际端口以 `.env` 中 `WANSHITONG_PORT` 为准。部署脚本会把一次有限的 app
日志快照写入 `/data/tyf/wanshitong/logs`，模型配置的两次非敏感 JSON 报告
写入 `/data/tyf/wanshitong/ops`。

## 配置与更新

修改 `.env` 前先保存变更原因，不能在不清楚绑定状态时更换模型 URL、模型名、
Embedding 维度或 Credential。内网模型绑定检测到漂移会明确失败，不会静默
改绑。

更新应用时先加载新镜像、修改 `RAG_APP_IMAGE`，再执行 `preflight.sh` 与
`deploy.sh`。不要使用以下命令：

```text
docker compose down
docker rm
docker volume rm
docker image rm
docker system prune
```

## 导入恢复

导入器按“规范相对路径 + 文件 SHA256”生成稳定 Idempotency-Key。中断后使用
同一 Manifest、同一语料根和 `--resume --wait` 重跑：

- 已可检索文档会跳过；
- `queued` / `running` Job 会继续轮询；
- `failed_retryable` 只重试一次；
- terminal failure 会保留已成功文档，并在报告中列出安全错误；
- 不会直接写数据库或 Qdrant，也不会删除文档。

上传入口每个来源地址每分钟最多十次。导入器遇到 429 会遵守有界
`Retry-After` 后重试一次，因此全量导入可能持续数分钟。

模板路径（目录或文件名含“模板”）经上传校验后由服务端转成只含动态标题的
目录项 DOCX；原始模板正文不会进入新版本的检索索引。既有语料切换策略时，
在新应用部署后运行导入器的 `--refresh-templates --wait`，它只给既有模板
创建目录项版本，不删除逻辑文档或非模板版本；完成后核对活动索引中模板
Chunk 仅含目录项，再做公共问答回归。原始源文件仍保留在受控语料目录。

OPC 模板目录另有一份旧 `.doc`，不属于 46 项 DOCX 正文控制清单。使用
`catalog_legacy_template.py prepare` 在本地语料根确认它存在，只根据路径和
文件名生成全新 `.docx` 目录项（不读取旧 DOC 正文）；产物经 54 转到 60 的
`/data/tyf/wanshitong/artifacts`。在 60 使用同脚本的 `upload` 子命令、管理员
Bootstrap Token 文件和本机 API 注册；脚本等待 Lifecycle Job 完成并写安全
报告。其可检索路径是原路径加 `x` 的目录项别名，标题标注“原件 .doc”；
不得把原 DOC 当作正文上传，也不得改变 `DOCX_ONLY_MANIFEST_46.json`。

## Secret

Secret 目录必须是 0700，文件必须是 0600。只查看 Bootstrap Token 时使用：

```bash
sudo sh -c 'umask 077; exec cat /data/tyf/wanshitong/secrets/admin-bootstrap-token'
```

不要把命令输出粘贴到日志、工单、Git 或最终部署报告。若 Secret bundle 只剩
部分文件，不要补写或覆盖；先停止依赖步骤并调查文件来源。

## 受控 TCP 转发

如果跳板机没有 `socat`，可把 `tcp_forward.py` 复制到跳板机专用的
`/data/tyf/wanshitong-access`，以前台进程或现有进程管理器运行。脚本只接受
精确私网/回环 IP，拒绝 `0.0.0.0`、主机名和公网地址；日志仅记录监听与目标，
不记录转发内容。PID、日志和现场启动脚本均应留在该专用目录。启动前必须先
用 `ss -lnt` 确认监听端口空闲，并从实际来源主机完成合同探测。

## 诊断顺序

真实问答失败时依次核对：Public Session/Cookie/HTTP 边界、固定 Scope、活动
Index Revision、Embedding 维度、Reranker 协议、Generation Connection、
Provider validation、实际 Provider Trace、检索候选、Evidence/Claim Validation
和前端 SSE。不得通过关闭引用校验、跳过 Reranker 或加入固定答案来制造通过。
