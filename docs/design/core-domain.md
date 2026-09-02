# Core 领域模型与边界

## 依赖方向

`rag_app.core` 只使用 Python 标准库和 Pydantic v2。`rag_app.application` 只依赖
Core 模型、错误和 Ports。FastAPI、httpx、Qdrant Client、DOCX/lxml、厂商 schema 与
具体 adapter 都位于外层；Composition Root 负责把实现注入同步应用核心。

## 模型规则

- 公共 schema 统一 `extra="forbid"`、`frozen=True`，集合使用 tuple；JSON metadata
  在入口规范化为按键排序的 tuple。
- 时间必须带时区；整数预算使用 `StrictInt`，避免布尔值被当作整数。
- `SecretRef` 只保存受控环境变量名，Core 不保存或读取 secret 值。
- 文档正文、向量和二进制字段从 `repr` 隐藏；Trace 只接收结构化安全属性。
- Error code 是稳定机器值，`str(error)` 只输出 code 与 `safe_message`，details 拒绝
  API Key、响应体、SQL、文档正文和文件路径字段。

## 身份

逻辑资源使用 `prj_`、`kb_`、`doc_`、`job_`、`trace_` 随机身份；内容和结构版本使用
`dver_`、`node_`、`chunk_`、`irev_` 确定性身份。规范化 JSON 使用 UTF-8、Unicode
原文、排序键、紧凑 separators 和拒绝 NaN/Inf 的 SHA-256；Python `hash()`、绝对路径、
显示名和数据库自增值不参与。

## Embedding 主备

`EmbeddingTopology` 明确 primary/standby、role、Provider/model、named vector、维度、
document/query policy 和 normalization。Jina `dense_primary` 与 Qwen3.7
`dense_standby` 即使均为 1024 维，`vector_space_identity` 仍不同。EmbeddingRequest、
EmbeddingResult、VectorWriteRequest 和 VectorSearchRequest 都必须携带 slot；Memory
Store 按 revision/slot/vector name 三元组隔离，找不到精确空间时 fail closed。

## P01 用例边界

P01 `RagEngine` 支持从已装配实例构造、由宿主注入 builder 从 Profile 构造、组件信息、
不联网 health 和幂等 close。`ingest/search/answer` 还未迁移时抛出
`CAPABILITY_UNAVAILABLE`，不会返回固定成功。旧 QueryService 与 HTTP API 保持原签名。
