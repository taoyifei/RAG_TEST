# P11 Jina 首次真实调用网络失败

状态：`BLOCKED_AFTER_FIRST_LIVE_ATTEMPT`。

发现日期：2026-09-04。

## 触发条件

发布候选镜像已完成离线验收并原位部署，运行时 OCI revision 为
`88a260263db425e645a7ef759106342fa4b9d95f`。重部署前的统一备份
`pre-88a2602.tar.gz` 已通过校验，SQLite integrity 为 `ok`；只重建了 app，
Qdrant 容器 ID 与启动时间保持不变。数据库仍有两个页面加密托管的 Provider
Connection 和两个 Credential。

首次真实调用前再次声明并检查了收紧预算：最多 25 次 Provider HTTP 尝试、全局
最多 1,000 估算输入 Token、每个 Provider 最多 600。五项页面连接验证的静态计划
为 5 次请求、231 估算输入 Token，其中 Jina 100、百炼 131。只允许代码内固定的
公开合成文本，不发送企业文档、真实知识库、Secret 或 Provider 响应正文。

## 实际结果

按失败即停规则，运行到第一项即停止：

```text
Provider: Jina
Operation: embedding.document
Model: jina-embeddings-v5-text-small
HTTP attempts: 1
Estimated input tokens: 19
Observed provider tokens: unavailable
Status: failed
HTTP category: network_error
Safe error: PROVIDER_NETWORK_ERROR
Latency: 3250 ms
```

这条失败已经持久化到 `provider_validation_runs`。其余 Jina query、Jina reranking、
百炼 document/query、公开合成 DOCX 双槽索引、查询、故障切换与恢复均为
`NOT_RUN`。百炼实际请求为 0。指定的 `GM-03 质量管理制度.doc` 没有出网。

本次累计账本为 1/25 次 Provider HTTP、19/1,000 估算输入 Token；Jina 为
19/600，百炼为 0/600。没有自动重试，也没有用 Mock 结果替代失败。

## 无额外 Provider HTTP 的诊断

- 容器 DNS 能解析 `api.jina.ai`，返回 IPv4 地址；
- 到 `api.jina.ai:443` 的 TCP 连接成功；
- 默认 CA 校验下 TLS 1.3 握手成功；
- Runtime 中 `httpx==0.28.1`，CA bundle 存在；
- 容器没有 HTTP、HTTPS 或 ALL_PROXY 环境变量；
- 应用日志只记录本地 API 路径和状态，没有请求正文、密钥或 Provider Body。

这些证据只能排除基础 DNS、TCP 和 TLS 建连故障，不能把模型 HTTP 调用判为成功。
当前持久化安全错误不足以区分远端主动断开、瞬时 HTTP 传输故障或其他
`httpx.RequestError` 子类；在不增加第二次 Provider HTTP 请求的前提下无法进一步
缩小。

## 决策

1. 立即暂停全部真实 Provider 调用，不顺带测试百炼，不重试 Jina。
2. `LIVE_JINA_EMBEDDING_READY`、`LIVE_JINA_RERANKER_READY`、
   `LIVE_QWEN_STANDBY_READY`、`AUTOMATIC_FAILOVER_READY` 与
   `REMOTE_PRODUCTION_PROFILE_READY` 保持 `false`。
3. 私有 DOC 完整双槽发布仍单独受 1,000/600 Token 硬上限阻断，不截断文档，
   不减少槽位，不以局部样本冒充完整发布。
4. 后续只有在明确决定进行一次受控重试或先增强安全的传输错误分类后，才恢复
   Live Gate；所有尝试继续累计计入同一 25/1,000/600 账本。
