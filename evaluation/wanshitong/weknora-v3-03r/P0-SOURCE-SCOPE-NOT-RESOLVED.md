# P0：命名规范短问在生成前无法绑定来源

## 隔离复现

V3-03R 的 8289 候选镜像 `sha256:b66d98324fcc5494f198ba9d9bc227639db5ed2e641a878d541e95d9c9c69747`，四份原始 DOCX 已完成入库，活动索引 `irev_080fea5bc9f679e22226a40b581bc1b8`。通过管理员候选入口，`engine_id=wk-standard-pc-v1`、`rewrite_enabled=false` 提问：

> 研发项目命名规范中，研发项目名称由哪些要素组成？

两次均在约 0.2 秒后返回 `SOURCE_SCOPE_NOT_RESOLVED`：`trace_fe566b30767141d885e0571fa7f6ee64` 和 `trace_9a1b4f0c2bbb433cbc1aa28dde2e59889`。代码抛出位置为 `WeKnoraPipeline` 的 `retrieval.source_scope`，在重排和自然生成之前。另一个短问、两个长问及无答案问可进入自然生成，因此不能将其归为 Provider 流格式问题。

## 影响和后续边界

该问不能到达模型，也无可发布答案。当前未确定是标题别名歧义、来源目录或索引元数据问题；四份文档入库成功并不能证明来源解析必然正确。V3-03R 明确禁止调整召回和来源限定，本轮只记录，不通过放宽来源隔离绕过失败。后续在相同索引和同一问题上检查活动版本的来源目录、标题映射和 `resolve_query_source_context()` 决策，再用相同请求验收。
