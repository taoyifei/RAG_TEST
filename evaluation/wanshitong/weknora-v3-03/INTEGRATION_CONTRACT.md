# V3-03 接入合同

基线：`9590fffdaa4116f08ea266832b7f8fe78ba78393`；固定参考：`Tencent/WeKnora@1edcd54b43606d9079bb36650efe3f68707a79ea`。本轮只改湾事通正常页面的自然问答接入，18288 不发布。

| 边界 | 合同 |
|---|---|
| 旧公共协议 | 不发送 `X-Wanshitong-Stream-Protocol` 时仍为 `wanshitong-public-sse-v1`，原 `meta/stage/claim/final/error/cancelled` 语义不变。 |
| 新公共协议 | 候选环境显式启用且客户端发送 `wanshitong-natural-sse-v1` 时，服务端使用固定 `wk-standard-pc-v1`。`answer_delta` 始终是临时正文，只有 `final` 的有效引用答案可入成功历史。 |
| 终态 | `final/error/cancelled` 至多一个。`missing/invalid/truncated` 不输出管理员草稿，也不写入成功会话。网络断线可能使客户端收不到已完成终态，可按 turn ID 查已提交轮次。 |
| 会话 | 自然轮次使用独立加密表；旧逐 Claim 表不改。owner、Project、KB、conversation、活动 Revision 和来源 Chunk 一并复核，TTL 和最多 8 轮沿用已有策略。 |
| 引用 | 实际送模材料的 S 别名只在本 turn 生效；公开 `reference_id` 由 trace 与别名派生。下载按 owner、turn、文档、版本和 Artifact 再鉴权，原件缺失时明确失败。 |
| 停止 | 前端 Abort → SSE 迭代器取消 → Provider `StreamCancellation`。有界队列满时可响应取消。收到首段正文后不切换模型。 |
| 测试资源 | 只在 8289 使用新镜像、独立数据目录和独立索引；保留原 8289 容器供回滚。验收后按精确身份清理新增容器、镜像、索引和目录并恢复原 8289；18288 只读核对。 |

验收：正常页面真实增量、服务端追问及历史、来源访问、无效引用不发布、旧协议兼容、8289 回滚和 18288 不变。准确度观察单独记录，不以历史阻塞题全对为准出条件。
