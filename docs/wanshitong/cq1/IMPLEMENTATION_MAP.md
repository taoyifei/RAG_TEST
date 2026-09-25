# CQ1 实现映射与准出边界

基线：`dca24a80823b4e5d24dbbdd725e10c97167ad7a5`；本地隔离分支：`codex/cq1-grounded-answer`。本目录的 `SOURCE_LOCK.json` 是用户交付的原始设计时快照，其 `plan_only` 状态不代表本轮执行结果。

| 范围 | 代码和测试 | 当前证据 |
| --- | --- | --- |
| CQ1-0 来源与模板复现 | `natural_source_scope.py`、`test_natural_source_scope.py`、`TEMPLATE_DATA_AUDIT.md` | 《研发管理计划》短名未登记受信别名会澄清；源范围贯通到实际检索过滤。原 DOCX 与当前活动目录提示的六层差异已审计。 |
| CQ1-1 来源范围 | `natural_source_scope.py`、`weknora_pipeline.py`、`search.py`、`public_models.py` | 书名号文档在原问任意位置冻结唯一版本；服务端选择优先；弱措辞仍软提示；用户显式 `source_mode=open` 可解除本轮提及限定；改写不能增加授权。 |
| CQ1-1 模板原件 | `template_catalog.py`、`template_parser.py`、`catalog_legacy_template.py` 及模板测试 | DOCX 不再改写成目录提示；安全 Parser 保留真实 IR、阅读域、来源跨度，Parser 指纹升 v3；旧 `.doc` 仍仅有目录存在性提示。当前服务器旧活动索引未重建。 |
| CQ1-2 草稿、核查、引用 | `natural_answer.py`、`natural_support.py`、`weknora_pipeline.py`、`service.py`、`test_natural_support.py` | 严格候选每轮至多一次草稿与一次批量审核；草稿不下发公共流。原文摘录逐字、唯一位置、SourceSpan 和版本校验，部分单位确定性发布；预算、截断和非法格式闭锁。模型资格失败，候选禁用。 |
| CQ1-3 历史与流 | `product/conversations.py`、`wanshitong/natural_stream.py`、`public_api.py`、`weknora_query.py`、前端 `src/public/` 与对应测试 | v2 成对角色历史只取已批准答复；run/owner/sequence 有界重放、刷新续接、显式 stop、截止时间和唯一终态；来源卡用审核摘录与单位定位。均为离线合同，未做普通页面实测。 |
| 准出开关 | `settings.py`、`bootstrap.py`、`compose.candidate.yaml` | `RAG_WANSHITONG_NATURAL_PUBLIC_V2_ENABLED` 默认 `false`，且需原自然流开关为 true。当前未在服务器设置或启用。 |

未改变召回通道、RRF、Reranker 协议、Go 二进制、模型服务或生产镜像；没有引入逐 Claim 链、PDF/OCR 或全量腾讯迁移。`SOURCE_LOCK.json` 中的腾讯版本只作为固定的 UX 参考，不是语义正确性证明。
