# 配置模型服务

管理员登录后打开“模型服务”。先建立 Jina 主连接，再建立阿里云百炼备用连接。

页面加密托管要求服务启动时已配置 `RAG_MASTER_KEY_FILE`。输入的新密钥只用于当前提交，
不会回显；部署环境托管则只填写环境变量名，由服务端在调用边界解析。

Jina 依次执行文档向量、查询向量、结果重排；百炼依次执行文档向量、查询向量。每项验证
都必须成功后，双槽检索方案才可激活。验证只说明连接与响应合同在当前环境通过，不代表
远程生产质量、容量或校准已经完成。

轮换页面托管密钥后，请重新运行引用该 Credential 的全部连接验证。轮换不会触发索引
重建；修改 embedding 模型、维度或指令才会要求新索引版本。

## P11 Live Gate

默认真实验收只发送公开合成短文本与由其生成的 DOCX。本次任务另有一项显式例外：
只允许发送授权决策中点名的 `GM-03 质量管理制度.doc`，不扩展到其他企业文档或真实
知识库，也不发送 Secret、向量或 Provider 原始响应体。手工工作流要求 Environment
审批和显式授权短语；外层上限为 30 次 HTTP 尝试和 20,000 估算输入 Token，本次
执行已进一步收紧为 25 次、全局 1,000、每 Provider 600。重试也计入请求和 Token
预算，超限会在下一次请求发出前失败；点名 DOC 的完整双槽发布当前因此被预算阻断。

验收依次覆盖 Jina document/query Embedding、Jina Reranker、百炼
document/query Embedding、真实 Qdrant 双槽索引、正常 Primary 查询、测试内
Acceptance Proxy 阻断 Jina Query、Standby 切换和 Half-open 恢复。故障注入只存在
于测试代码，普通 Product Runtime 没有强制故障开关。
