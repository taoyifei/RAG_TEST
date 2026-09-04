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

真实验收只发送公开合成短文本与由其生成的 DOCX，不发送企业文档、真实知识库、
Secret、向量或 Provider 原始响应体。手工工作流要求 Environment 审批和显式授权
短语，请求上限为 30 次 HTTP 尝试，估算输入 Token 上限为 20,000。重试也计入请求和
Token 预算，超限会在下一次请求发出前失败。

验收依次覆盖 Jina document/query Embedding、Jina Reranker、百炼
document/query Embedding、真实 Qdrant 双槽索引、正常 Primary 查询、测试内
Acceptance Proxy 阻断 Jina Query、Standby 切换和 Half-open 恢复。故障注入只存在
于测试代码，普通 Product Runtime 没有强制故障开关。
