# Retrieval Profile Revision

Provider Connection 是全局可复用连接；Retrieval Profile Revision 是知识库级不可变
配置版本。Draft 经连接验证、影响预览和用户确认后原子激活，原 Active Profile 转为
retired。既有 Active Index 不被原地修改。

## 双 fingerprint

Index semantic fingerprint 包含 embedding provider/model/dimension、document/query
policy、指令、归一化含义、主备拓扑、chunker 和 FTS analyzer。上述任一语义变化均返回
`NEW_INDEX_REVISION_REQUIRED`。

Serving fingerprint 在 index fingerprint 之上加入 reranker、RRF、证据与置信度策略、
预算等服务参数。只改变这些参数返回 `SERVING_RELOAD`；两个 fingerprint 都不变时返回
`NO_REINDEX`。

API Key、Credential ID/version 和连接显示名不进入 fingerprint。Credential 轮换不会
触发重建索引。

## 激活门禁

双槽方案要求 Jina document/query embedding、Jina reranker 和百炼 document/query
embedding 五个操作都存在当前 Credential version 的成功验证记录。页面必须先显示影响，
提交激活时再带回相同的确认值。P10.5 只验证配置闭环；需要新索引时由 P11 接入真实
Provider 数据面后执行 build、validate 和 atomic activate。
