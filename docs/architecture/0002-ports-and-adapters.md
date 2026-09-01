# ADR 0002：Ports、Adapters 与可信注册

- 状态：Accepted
- 日期：2026-09-01

## 决策

通用核心通过窄接口表达能力，基础设施在 composition root 注入实现。后续最小端口按
职责分为：

- `ParserPort`：输入受控字节/流和格式元数据，输出格式中立 Document IR；DOCX 安全
  parser 是首个 adapter。
- `ChunkerPort`：只接收 Document IR 和冻结策略，返回携带稳定来源跨度的 chunk。
- `EmbeddingPort`、`RerankerPort`、`GenerationPort`：使用通用请求/结果对象，不向
  core 暴露 OpenAI、TEI、Qwen 或其它厂商 HTTP schema。
- `VectorStorePort`、`LexicalStorePort`：表达写入、查询、发布和身份校验；Qdrant 与
  SQLite FTS5 分别是 adapter。
- `TracePort`：只接收脱敏结构事件；正文捕获、保留期和导出由 adapter 策略决定。

端口只描述本项目已经需要的行为，不提前为未知 Provider 建立大而全抽象。

## 依赖方向

```text
API / CLI / host application
            |
      composition root
       /      |       \
  adapters -> ports <- universal core
```

`rag_app.core` 不得 import `rag_app.api`、FastAPI、Qdrant Client、`rag_app.clients` 或
OCR/具体 Provider。`rag_app.application` 只依赖 core models/ports，同样不得依赖 API、
HTTP 客户端、Qdrant、Parser 实现或具体 adapter。阶段 0 的静态边界测试会在这些包
出现后自动扫描反向依赖。

## 同步应用语义

- V1 的 RagEngine/Application Service 和所有端口保持同步接口。
- HTTP Provider adapter 复用有连接池的同步 `httpx.Client`，不在每次请求创建客户端。
- FastAPI 外壳使用普通 `def` 路由或受控线程池调用同步用例。
- 禁止在同步实现中调用 `asyncio.run()`、隐式创建事件循环或另建重复 async 核心；若
  后续真实吞吐证据要求异步端口，必须先新增 ADR。

## 插件选择

- Parser、Chunker、Provider 和 Store 只能由代码显式 Registry 注册，或由可信配置从
  已注册名字中选择。
- Registry 的键和值都必须在启动时校验；未知名字失败关闭。
- 禁止把用户输入交给 `importlib`、`__import__`、`eval`、entry-point 字符串或任意
  Python 路径执行。
- 外部实现若需扩展，先由维护者安装并显式注册；动态发现不是默认能力。

## 索引 revision 与两类指纹

Parser、ParsingPolicy、Document IR 规范化、Chunker、Token Budget、Embedding、词法
schema、向量 schema 或 source span/chunk ID 规则发生变化时，必须创建新的
`IndexRevision`，完整构建后原子激活。不得让新维度或新语义的查询向量读取旧
collection。

`index_fingerprint` 至少覆盖 parser 与 parsing policy、IR schema、enricher、chunker
及参数、token counter 的 exact/estimated 身份、Embedding 模型/修订/维度/instruction/
normalization、词法 tokenizer/schema、向量 distance/schema 和 chunk payload schema。

`serving_fingerprint` 至少覆盖 query analyzer/planner、query expansion、检索通道、融合
方法/权重/k、reranker、邻块/父块扩展、证据预算与多样性、置信/拒答策略以及 generator/
prompt/citation protocol。查询时替换 Reranker、Generator、Planner、Fusion、Evidence
Packer 或 Trace Sink 通常只改变 serving fingerprint，不复用错误的 serving cache。

两类指纹都使用字段排序后的规范化 JSON 和 SHA-256；禁止把绝对路径、字典偶然顺序或
secret 纳入指纹。稳定逻辑 ID 与显示名分离，文件重命名不得改变 `document_id`，内容
变化必须创建新的 `document_version_id`。

## 迁移方式

现有 FastAPI、Qdrant、DOCX 和模型客户端继续工作。后续阶段从 composition root 向内
逐个提取端口，并为每次兼容迁移保留当前行为回归；不进行一次性目录重写。

下一步目录只在现有 `rag_app` 下建立：`core/models`、`core/ports`、`core/errors.py`、
`core/fingerprints.py`、`core/policies.py`，以及 `composition/registry.py`、
`composition/profiles.py`、`composition/factory.py`。旧实现通过 legacy adapter 绞杀式
迁移，本阶段及下一阶段均不改旧公共 API 或已有索引。
