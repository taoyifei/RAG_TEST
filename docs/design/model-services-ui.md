# 模型服务页面设计

页面只显示版本化 Catalog 中的 Jina 与阿里云百炼，不提供自定义 Provider、模型或 Base
URL 输入。Jina 连接复用同一 Credential 承担文档向量、查询向量和结果重排；百炼连接
固定 `cn-beijing`，并保存非 Secret 的 workspace、请求与 token budget。

建立连接时先选择“页面加密托管”或“部署环境托管”。密码输入关闭自动完成，提交成功后
立即清空。列表只显示 masked hint 和版本，页面没有回显旧值的能力。

五个验证按钮彼此独立。验证只发送公开合成文本，页面展示 operation、model、status、
HTTP category、dimension、token 和 latency 的安全摘要；Raw Error Code 只在技术详情中
出现。轮换页托管密钥后，连接需要重新验证，但检索方案 fingerprint 保持不变。

视觉采用浅色工作区、白色卡片、企业蓝、紧凑控件、弱阴影和明确 focus ring。没有引入
私有包、内部 API、私有字体或图标。
