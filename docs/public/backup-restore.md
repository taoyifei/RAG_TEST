# 备份与恢复

统一备份包含 SQLite 一致性快照、Blob、活动 Qdrant Collection Snapshot、
Compatibility Manifest、成员 SHA 和 Backup Manifest。主密钥、Bootstrap Token、
Qdrant Key 与 `qdrant.yaml` 被明确排除。

本机受控试用只接受由可信本机管理员创建的备份。创建流程把完整归档的 SHA-256
收据另存于 `RAG_DATA_DIR/backup-trust`（容器内为 `/data/backup-trust`）；目录必须由
当前用户持有且为 `0700`，收据必须为 `0600`。未设置 `RAG_DATA_DIR` 时使用
`~/.local/share/rag-app/backup-trust`。该目录不包含在普通归档中，应另行保管。
验证和恢复先复制归档到私有临时目录、核对完整字节的外部收据，再打开其中的 SQLite；
恢复全过程使用该副本，不重新读取原归档路径。

归档内 Manifest 与成员摘要不证明来源可信。未知归档、缺失创建收据的旧备份和
被篡改的归档均被拒绝，不得从待验证归档自行生成信任收据。当前没有任意归档的
自动信任入口。跨主机恢复还需管理员独立核实并安全迁移真实创建收据及其权限。
这只是限制不可信数据库进入的风险缓解，不是 SQLite 漏洞修复；可信本机管理员
仍可修改持久数据库及收据。

## 创建与校验

先停止写流量和索引任务，再在应用容器内执行：

```bash
docker compose exec app rag-app backup create \
  --data-dir /data \
  --output /data/backups/p11-backup.tar.gz \
  --compatibility-manifest /app/compatibility-manifest.json
docker compose exec app rag-app backup verify \
  --archive /data/backups/p11-backup.tar.gz
```

命令默认读取容器中的 `RAG_QDRANT_URL` 与
`RAG_QDRANT_API_KEY_FILE`。把归档复制到独立存储，并另外备份 `rag_secrets` 中的
主密钥。仅有真实外部创建收据的归档可进入数据库完整性校验；没有原主密钥就无法
解密恢复后的页面托管凭据。已有但缺失来源证明的备份保留原件，不能猜测为可信。

## 非覆盖恢复

恢复目标必须是新的空数据目录和新的 Qdrant 实例。命令拒绝覆盖已有目录或
Collection：

```bash
rag-app backup verify --archive /backup/p11-backup.tar.gz
rag-app restore \
  --archive /backup/p11-backup.tar.gz \
  --target-data-dir /restore/data \
  --qdrant-url http://restore-qdrant:6333 \
  --qdrant-api-key-file /restore/secrets/qdrant-api-key
```

启动恢复环境后检查 SQLite integrity、项目/知识库/文档/版本/任务、连接验证、
Profile、活动 Revision、向量 Inventory、FTS 查询、引用和 API Token 元数据。真实
Provider 查询只在用户重新授权且原主密钥可用时运行。
