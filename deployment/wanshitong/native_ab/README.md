# 湾事通原版 WeKnora 隔离 A/B

本目录是试验工具，不接入湾事通生产路由，也不替换 Q1 自然问答算法。A 固定为 `dca24a80823b4e5d24dbbdd725e10c97167ad7a5`；B 固定为 Tencent/WeKnora v0.8.2 的 `3e8b0bfc80b845b2d4b2ed683994748741450a97`。上游源码独立存放，业务原件、题库、令牌、完整 SSE 和评分留在受限目录，不提交 Git。

## 运行边界

- A 与 B 使用不同 Compose project、网络、存储、账号和回环端口；保护原 8289 与 18288 容器及镜像。
- B 使用原生 `KnowledgeQA` 路由。`agent_enabled=false`；原生 quick-answer 配置提供模型和重排 ID，不进入 ReAct Agent。
- 两侧读取相同原始 DOCX 字节及同一内网 LLM、Embedding、Reranker 端点。各自用原生解析和索引，保留原生引用与发布行为。
- `rerank_adapter.py` 只转换 WeKnora Cohere 协议到现有 TEI 请求/响应形状，不改变顺序、下标或分数语义。
- 主试验先冻结题库与配置，再运行开发集和留出集；首次失败原样保存，不用重试覆盖。

## 文件用途

| 文件 | 用途 |
|---|---|
| `model_contract_probe.py`、`test_rerank_adapter.py` | 验证三模型实际接口及薄适配层 |
| `compose.b.override.yaml`、`prepare_b_config.py`、`prepare_b_runtime.py` | 在独立上游 checkout 中准备 B 的固定配置和隔离服务 |
| `Dockerfile.a`、`compose.a.yaml`、`prepare_a_runtime.py` | 从受保护 8289 镜像派生 A 的独立试验镜像与服务 |
| `import_ab_corpus.py`、`extract_source.py` | 原件哈希锁定、各自导入、原文定位提取 |
| `stage_corpus_evidence.py` | 按哈希复制原始 DOCX 到受限归档，保持可复核性 |
| `smoke_ab.py`、`smoke_batch.py` | 12 题协议接通冒烟 |
| `replay_ab.py` | 60 场景的会话级随机配对主回放 |
| `stability_ab.py`、`performance_ab.py` | 12 题三次重复及 1/2/4 会话分时测量 |
| `make_review.py`、`summarize_ab.py` | 生成匿名及具名对照、结构指标；正确性须由人核查 |
| `score_review.py` | 读取用户导出的具名评分，计算完整场景的配对胜负与差值区间 |
| `capture_runtime.py` | 记录实验与受保护容器身份、健康和输入哈希 |

服务器上的私有题库遵循 `04_用例标注模板.jsonl` 形状，每条证据都指向原始 DOCX 的定位和摘录 SHA-256。代码仓库只保留脚本和脱敏报告；运行实例、原始材料和令牌不随提交传播。

## 本次已验证的关键配置

上游默认重排模型不会自动绑定到主 KnowledgeQA 会话。B 通过上游现有 quick-answer 配置给原生会话指定聊天与 Reranker 模型；Embedding 由知识库模型配置提供，并用 `agent_enabled=false` 调用标准路由。B 默认 1024 输出的开发冒烟曾触发 8192 上下文超限，主配置固定 `rerank_top_k=6`、`max_completion_tokens=2680`、`temperature=0`、`fallback_strategy=fixed`，不改上游 Go 问答源码。A 使用现有 Q1 配置。

完整执行身份、输入哈希、请求结果、引用支持和清理结果见同分支的试验报告。发布或迁移不属于本目录的自动动作。
