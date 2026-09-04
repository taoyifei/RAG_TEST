# 中文文案规范

主流程使用用户任务语言，不直接展示阶段号或内部 schema 名。统一字典放在
`frontend/src/copy/zh-CN.ts`，导航、状态和常见空状态从字典读取。

- Project 显示为“项目”。
- Knowledge Base 显示为“知识库”。
- Active Revision 显示为“当前索引版本”。
- Provider Probe 显示为“连接验证”。
- SourceSpan 显示为“来源位置”。
- Primary/Standby 状态显示为“主连接/备用连接验证”。
- Evidence 与 fingerprint 等工程信息只进入可展开技术详情。

原始 ID、JSON、HTTP category 和 safe error code 可以保留，但不得替代面向用户的中文
结论。状态标签必须同时依赖文本、颜色和可访问名称。页面不得根据 Mock 成功显示“生产
可用”或类似结论。
