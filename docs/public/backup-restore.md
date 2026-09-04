# 备份与恢复

统一备份包含 SQLite 一致性快照、Blob、活动 Qdrant Collection Snapshot、
Compatibility Manifest、成员 SHA 和 Backup Manifest。主密钥、Bootstrap Token、
Qdrant Key 与 `qdrant.yaml` 被明确排除。

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
主密钥。普通归档可以安全校验，但没有原主密钥就无法解密恢复后的页面托管凭据。

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
