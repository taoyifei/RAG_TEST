# 知识引擎实现

## 从原件到索引

1. [文档解析服务](../vendor/knowledge-engine/docreader/)和 [Go 解析器](../vendor/knowledge-engine/internal/infrastructure/docparser/)提取原件的正文、结构和图片。图像处理能力取决于解析方式和配置的图像模型。
2. [文档分块](../vendor/knowledge-engine/internal/infrastructure/chunker/)按文档结构与参数组织片段，保存片段和原件的关联，为引用、预览和下载提供定位。
3. [PostgreSQL 检索实现](../vendor/knowledge-engine/internal/application/repository/retriever/postgres/)使用全文与向量索引，支持关键词与语义召回。

## 从问题到回答

1. [问答网关](../src/industrial_agent/app.py)读取当前身份并计算可访问的资料范围。指定文件问答使用文件级范围，公开、个人和内部资料的授权在每轮请求时校验。
2. [混合检索](../vendor/knowledge-engine/internal/application/service/knowledgebase_search.go)调用关键词与向量检索通道。[多路融合](../vendor/knowledge-engine/internal/application/service/knowledgebase_search_fusion.go)根据各通道的候选名次和权重合并结果。
3. [问答流程](../vendor/knowledge-engine/internal/application/service/chat_pipeline/)组织查询、候选片段和回答生成。[重排模块](../vendor/knowledge-engine/internal/reranking/)提供重排服务接入。
4. 网关将流式回答和有权查看的片段转给界面。展开引用后可以查看片段、预览图片并下载附件。

引擎和解析器可从当前分支源码构建，版本来源见 [upstream.lock.json](../upstream.lock.json)。
