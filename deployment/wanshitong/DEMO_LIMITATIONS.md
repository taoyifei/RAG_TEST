# 湾事通内网 Demo 边界

该部署用于受控私网功能演示，不等同于生产发布。

- 入站使用显式的湾事通 Demo HTTP 模式，只允许精确 Trusted Origin 与私网、
  loopback 或显式受信任 peer；Universal 默认 TLS 策略没有关闭。
- 当前只有一个隐藏 Project/Knowledge Base，公开查询固定为
  `visibility_scope=all_internal`、空部门与空快捷入口。
- 原任务书的正文语料仍为控制清单中的 46 个原生 DOCX；PDF、旧 DOC 正文、
  Excel、ZIP、OCR、SSO 和完整 ACL 不在本期范围。
- OPC 模板文件夹内有 13 份 DOCX 和 1 份旧 DOC。前者属于 46 项清单，
  后者只新增由文件名生成的 DOCX 目录提示，原 DOC 正文不读取、不上传、
  不索引。全部 14 个模板只可提示存在并参考原件，不得把占位符或示例
  当作制度事实回答；登记后逻辑文档总数为 47。
- 当前每类模型只有一个经过真实验证的 Primary Endpoint。发现的其他 Endpoint
  只记录为候选，本 Demo 不实现 Provider Fleet 或自动故障转移。
- app 与 Qdrant 不使用 GPU；模型服务由现有内网设施提供，其可用性与容量不由
  本 Compose 管理。
- HTTP Cookie 在精确允许的 Demo Origin 下不设置 `Secure`。迁移到 HTTPS 后
  应关闭 `RAG_WANSHITONG_DEMO_ALLOW_HTTP` 并重新核对 Origin/代理边界。
- 本期只做短时、最多四并发的功能 Smoke，不提供长时间压力、灾备、HA、SAST、
  DAST 或大规模准确率评测结论。
- 无证据问题必须拒答、说明资料不足或要求澄清；不得用答案表、关键词规则或
  文档专用代码弥补检索与生成问题。
