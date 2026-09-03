# Python SDK

P09 的同步入口是 `rag_app.sdk.RagSdk`，由
`rag_app.composition.build_p09_runtime()` 装配。SDK 与 HTTP API 使用相同的
LifecycleService 和 P07 RetrievalService，不返回 SQLite Row、FastAPI Response
或 Qdrant 对象。

```python
from rag_app.composition import build_p09_runtime

with build_p09_runtime(
    "configs/profiles/dev-offline.json",
    data_dir=".data/p09",
) as runtime:
    project = runtime.sdk.create_project(
        "示例项目",
        idempotency_key="project-20260903",
    )
    kb = runtime.sdk.create_knowledge_base(
        project.project_id,
        "制度库",
        idempotency_key="kb-20260903",
    )
```

稳定 facade 提供 Project、Knowledge Base、Document/Version、Artifact、Job、
Search、Answer、Health 和 Diagnostics 操作。`close()` 与 Context Manager 均幂等；
runtime 在生命周期内复用 SQLite、Blob、Vector 和 Provider 资源。

Document/Version 创建方法返回持久 Job，调用方应通过 `get_job()` 轮询至
`succeeded`、`failed_retryable`、`failed_terminal` 或 `cancelled`。任务由有界 Worker
执行，重启后会从 SQLite 队列恢复，进程内队列不承担事实源职责。

默认 `dev-offline.json` 会在 P09 组合根显式提升为 P06—P09 所需的持久化
Parser、Chunker、SQLite FTS V2、SQLite Control 和 Filesystem Blob 组件，同时保留
调用方选择的离线 Provider、策略与 Profile ID。该路径不读取远程密钥且不访问公网。
