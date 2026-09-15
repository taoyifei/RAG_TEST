# 湾事通叠加架构

## 状态

本文记录目标架构，不表示 WB-00 已新增运行能力。WB-00 只增加合同文档和只读检查器，
现有 Runtime、API、数据库、前端与部署行为保持不变。

## 单一运行链路

```text
公共 / 或管理员 /admin
        |
        v
湾事通 Facade / route registrar / frontend shell
        |
        v
Universal ProductRuntime + RagSdk + P09AnswerStream
        |
        +--> 既有解析 -> Document IR -> Chunk -> Index Revision
        +--> 既有 Exact/FTS/Dense -> RRF -> Reranker
        +--> 既有 Evidence -> Grounded LLM -> Claim/Citation Validation
        +--> 既有 ProductQueryHistory / ProductTraceCoordinator
        +--> 既有反馈、缓存、Singleflight、备份与恢复
```

图中每条 Universal 链路都只有一个权威实现。湾事通 Facade 负责输入收敛和 Scope 注入，
不复制内部查询、Provider、检索、生成、History 或 Trace 逻辑。

## 湾事通壳层

后续阶段优先把新增代码放在 `src/rag_app/wanshitong/`，并保持边界窄小：

- 公共 `/`：无账号登录的同源公共问答入口；
- 管理员 `/admin`：收敛现有管理能力，不重建后台数据面；
- 固定隐藏 Scope：Project/KB 由服务端配置并注入，公共浏览器不可选择或覆盖；
- 单知识库文档分类：在既有文档/检索合同上使用元数据过滤；
- 新公共前端：只调用湾事通 Facade，不直连 Provider、Qdrant 或管理员 Token；
- 未来快捷入口：只保留扩展点，第一版不展示。

公共会话、History、Operational Trace、反馈与缓存使用 Universal 已有实现。7 天加密
History 是现有 `ProductRuntimeSettings` 的默认合同，不新增第二套存储。

## Provider 组合

湾事通通过 Universal 的 Connection 与 `api_base_url` 配置内网模型：

| 角色 | Industry 参考模型 | 首选单 Endpoint |
|---|---|---|
| Embedding | `Qwen3-Embedding-0.6B` | `http://10.242.180.60:8091` |
| Reranker | `Qwen3-Reranker-0.6B` | `http://10.242.180.60:8092` |
| LLM | `Qwen/Qwen3-8B-AWQ` | `http://10.242.180.57:8000` |

其余 Industry LLM 地址只作候选参考。除非 Universal 当前抽象可直接表达，否则不把
Endpoint 数组引入数据面，也不创建 Fleet Manager。后续运行配置必须先验证实际模型
名称与协议；WB-00 不连接这些地址。

## 部署与安全边界

- 目标为内网 HTTP 主机端口 `8188`，通过独立 deployment overlay 表达；WB-00 不修改
  Docker 或部署文件。
- 无登录仅适用于公共问答身份；长期 Query/Admin/Provider/OCR Token 留在服务端，
  浏览器只持有不泄露凭据的同源会话状态。
- Scope 和元数据过滤必须在服务端失败关闭，不能信任浏览器提交的 Project/KB。
- OCR 与 PDF 沿用 Universal 能力，具体边界见 [ocr-capability.md](ocr-capability.md)。

完整不可回退规则以 [universal-first.md](universal-first.md) 为准。
