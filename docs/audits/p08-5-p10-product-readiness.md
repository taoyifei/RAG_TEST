# P08.5—P10 产品就绪审计

## 审计基线

- Start SHA：`9ae80b1132578b5f9ddf8d3c3f49ccfdd6388b90`
- P08.5 merge：`2d2eeecbced072c6801b9d89fb1ebfb85dc28e94`
- P09 merge：`47d360ffe006659cb9f5a9d181e56f48b400eb40`
- P10 merge：`6ffbb3880bce0459661ef486a937d24aeffc566c`
- 三个 merge 均已用 `git merge-base --is-ancestor` 对远程集成分支验证。
- External services actually called：none。

## 开工事实

Start SHA 的 `rag-app serve` 调用 `cli.build_runtime()`，要求旧 Pipeline、
Retrieval、Tokenizer、模型 Endpoint 和 release revision 配置。它没有启动 P09
SDK/API 与正式 React 的唯一组合根。`create_p10_app` 只在
`src/rag_app/api/p10.py` 和对应 API 测试中使用，没有进入正式 CLI。

根 `Dockerfile` 与 `deployment/compose.yaml` 属于旧 Industry/OCR 发布链。Compose
同时装配 Qdrant、OCR、旧远程模型 Endpoint、应用和索引 Worker。Provider 配置来自
旧 JSON Profile 与环境变量，页面不能创建 Connection、Credential 或知识库级方案。

P10 页面把 Query/Admin Bearer Token 和 Scope 放进浏览器 `sessionStorage`。Scope
适合保留，但管理员 Token 生命周期不符合浏览器会话边界。页面仍直接显示阶段编号、
英文工程名、Live 结论和部分内部结构词。生产状态中的远程能力也含固定值，不能证明
最近一次 Provider 验证。

开工门禁实际通过：doctor；compile、Ruff、mypy、docstring 全量检查；`1340`
个默认离线测试；`71` 个 smoke；前端 install/lint/typecheck、`8` 个单元测试、
build；离线浏览器 `3 passed, 3 skipped`；`git diff --check`。浏览器入口实际使用
`--profile configs/profiles/dev-offline.json`。

## P10.5 变更面

- API：增加同源管理员 Session、Provider Catalog、Credential、Connection、验证记录、
  Retrieval Profile Revision、影响预览和接口访问 Token；原 P09 API 继续复用。
- Schema：OpenAPI 由正式 Product Runtime 生成，前端类型从同一快照生成。
- Migration：只新增 0011—0014，不修改 0001—0010。
- CLI：`serve` 改为 Product Runtime；旧入口变为显式 `legacy-serve`。
- Runtime：增加控制面、Credential resolver、Provider client registry、按知识库的查询
  与 Revision 构建解析钩子，以及动态状态 overlay。
- 兼容策略：启动只校验数据库、IR、Chunk 与 FTS 的兼容范围；source revision 只追溯，
  不再要求与运行目录 Git HEAD 完全一致。
- 部署：`deployment/product/compose.yaml` 成为最小产品合同；旧 Compose 显式调用
  `legacy-serve`，等待 P11 完成生产切换。

本阶段所有 Provider 验证均由 `httpx.MockTransport` 和公开合成文本完成。没有读取
真实密钥、企业文档或上传中台包，也没有访问 Jina、阿里百炼或 Qdrant Server。
