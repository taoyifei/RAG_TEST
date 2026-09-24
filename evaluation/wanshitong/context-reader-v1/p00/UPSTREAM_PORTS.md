# WeKnora v0.8.0 参考行为与移植边界

固定上游：`Tencent/WeKnora@1edcd54b43606d9079bb36650efe3f68707a79ea`（v0.8.0）。本项目采用独立 Python 实现，仅借鉴机制；不复制 Go 源码、默认阈值或运行依赖。

| 上游文件与函数 | 本项目吸收 | 主动差异 |
| --- | --- | --- |
| `internal/application/service/chat_pipeline/merge.go` `resolveParentChunks`、`mergeResults` | 命中后从权威来源回读父级/结构成员，按来源顺序组织 | 不原地覆盖 Chunk；强制校验活动 revision、文档版本、part/story 与物理表身份 |
| `merge_overlap.go` `chunkTrusted` | 以真实 source 坐标合并同节点重叠片段 | 用 DocumentIR 完整节点长度判定覆盖，不靠文本相似或无界拼接 |
| `merge_expand.go` 邻居扩展 | 结构不足时有界回读 | 不采用 350/850 rune 常量；不在完整来源节点中途截断 |
| `filter_top_k.go` 排序与截取 | 稳定 tie-breaker | 按完整来源组和实际发送预算选择，超限明确记录 |
| `into_chat_message.go` 上下文投影 | 复用本方 `EvidenceReadUnit` 短编号与来源映射 | 不复制其 prompt、FAQ 优先级或聊天历史持久化 |
| `rerank.go` 重排及 MMR | 仅作为对照依据 | 本期保持现有召回、RRF、重排协议与模型配置 |

上游 `merge_expand.go` 会对合并文本执行固定长度截断，`rerank.go` 在合并前已有 Top-K/MMR，`filter_top_k.go` 在合并后再次筛选。以上行为不能用作“完整材料必定入模”的保证。

许可：[WeKnora MIT LICENSE](https://github.com/Tencent/WeKnora/blob/1edcd54b43606d9079bb36650efe3f68707a79ea/LICENSE)。本期记录机制来源并独立实现；若后续复制上游实质代码，须另行保留适用版权和许可证通知，第三方组件另核许可。
