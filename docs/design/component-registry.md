# 显式 Component Registry 与 Composition Root

## Registry 安全合同

`ComponentRegistry` 构造时为空，只有可信代码调用 `register_builtin_components()` 或
逐项 `register_*` 才会出现组件。注册名必须符合小写短名 pattern；重复、未知、路径、
模块字符串和表达式全部失败关闭。Registry 不扫描目录、entry point，不调用
`importlib`、`__import__` 或 `eval`，factory 构造也不得执行网络。

每项注册保存 `ComponentDescriptor`、严格 Pydantic config schema 和 factory。配置先
完整验证并报告字段路径，再运行 factory。`list_components()` 只输出职责、名称、版本、
来源、mode 和 capability，不输出环境变量值或 secret。

## Profile 与优先级

P01 使用严格 JSON，避免引入 YAML 依赖。优先级为显式 Python instance override、受控
环境变量引用、Profile 文件、内置默认值。环境变量只保存名字；Profile 导出不读取值。

内置 Profile 有两套：

- `dev-offline` 使用 legacy DOCX/section adapter、Deterministic Embedding、Lexical
  Overlap Reranker、Memory vector/lexical、内存 SQLite metadata/trace、Local blob 和
  Extractive Generator。
- `jina-qwen37-hot-standby` 固定 Jina v5 small primary、阿里 qwen3.7 standby 和 Jina
  reranker。P01 的三个远程组件只声明身份与能力，调用会失败关闭，真实 HTTP 属于 P02。

## 构建和生命周期

`build_components()` 先验证 Profile、EgressPolicy 和 Registry，再依次构建 Store、
Parser/Chunker、Providers、Router、Reranker、Generator 与 Trace Sink，验证维度能力并
计算 index/serving 指纹。返回的 `RagComponents` 是显式字段 dataclass/context manager。
任一步失败会逆序关闭已经创建的不同实例；成功后 `close()` 也保证每个实例最多一次。

阿里自动备用只有在 query 总授权、阿里 query 授权和本地请求/Token 正预算同时满足时
才允许配置；P01 不读取 Key、不探测公网，也不声称免费额度受到 API 侧保证。
