# 本地数据目录

默认数据根为 `.data/`，测试必须通过 `--data-dir` 使用临时目录。目录不应提交到 Git。

```text
.data/
├── universal-rag.sqlite3
├── universal-rag.sqlite3-wal
├── universal-rag.sqlite3-shm
├── blobs/sha256/ab/<64-char-sha256>
├── tmp/
└── qdrant/
```

Blob 文件名只来自 SHA-256，不使用上传名。SQLite、Blob root、Blob 子目录和 Qdrant path
拒绝 symlink 或越界 locator。CLI 输出 ID、状态、计数和 hash，不输出正文、向量、Secret、
Workspace ID 或绝对内部路径。

开发时可用 Memory Vector 或 Qdrant local-memory。Qdrant local-path 用于本地持久化原型；
它和 embedded local-memory 都不是 Qdrant Server 的生产等价证明。
