# Changelog

## 0.1.0-rc1 - 2026-09-04

- 默认入口切换为 Product Runtime、三阶段非 root 镜像和 app + Qdrant Compose。
- 增加页面托管 Jina/阿里云百炼连接、持久验证、双槽索引、预算和切换观测。
- 增加统一备份/校验/非覆盖恢复、Schema 15 升级与回滚边界。
- 增加浏览器 Session、CSRF、TLS/Proxy、限流、API Token 与 Secret 门禁。
- 增加统一 build/verify/acceptance 命令、七类 CI 作业和受审批 Live 工作流。
- 增加 `.doc` 与 `.docx` 上传支持；旧版 DOC 使用固定本地转换器提取纯文本并明确
  标记结构降级，DOCX 继续使用结构化 OOXML 解析器。

此候选版本的真实 Provider、远端 CI、Branch Protection 与最终 P11 状态以
`docs/progress/phase-11.md` 的实际证据为准，不能由本变更记录推断为已通过。
