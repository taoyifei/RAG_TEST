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
OCR/具体 Provider。阶段 0 的静态边界测试会在 core 包出现后自动扫描这些反向依赖。

## 插件选择

- Parser、Chunker、Provider 和 Store 只能由代码显式 Registry 注册，或由可信配置从
  已注册名字中选择。
- Registry 的键和值都必须在启动时校验；未知名字失败关闭。
- 禁止把用户输入交给 `importlib`、`__import__`、`eval`、entry-point 字符串或任意
  Python 路径执行。
- 外部实现若需扩展，先由维护者安装并显式注册；动态发现不是默认能力。

## 迁移方式

现有 FastAPI、Qdrant、DOCX 和模型客户端继续工作。后续阶段从 composition root 向内
逐个提取端口，并为每次兼容迁移保留当前行为回归；不进行一次性目录重写。
