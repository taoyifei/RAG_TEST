# Provider 配置与检查

## 内置 Provider

P02 只提供以下真实远程组件：

| 注册名 | 模型 | 角色 | 默认传输 |
| --- | --- | --- | --- |
| `jina-embedding` | `jina-embeddings-v5-text-small` | document/query | Jina HTTPS API |
| `aliyun-qwen37-embedding` | `qwen3.7-text-embedding` | document/query | 北京 DashScope 原生 API |
| `jina-reranker` | `jina-reranker-v3.5` | reranking | Jina HTTPS API |

离线开发继续使用 `deterministic` 和 `lexical-overlap`。这些实现只验证接口、顺序、隔离和
错误路径，不代表语义质量。

## Profile

- `configs/profiles/dev-offline.json`：默认离线、无 Key、无公网；
- `configs/profiles/dev-jina-only.json`：Jina 单 Dense slot；
- `configs/profiles/dev-jina-qwen37-hot-standby.json`：Jina 主用、Qwen3.7 双索引热备、
  Jina Reranker。

Profile 只保存 `JINA_API_KEY`、`DASHSCOPE_API_KEY`、
`ALIYUN_MODEL_STUDIO_WORKSPACE_ID` 和 `ALIYUN_MODEL_STUDIO_REGION` 这些环境变量名，
不会读取或导出变量值。

## 离线命令

```bash
.venv/bin/python scripts/dev.py provider-list
.venv/bin/python scripts/dev.py provider-check \
  --profile configs/profiles/dev-jina-qwen37-hot-standby.json
```

`provider-check` 只验证 JSON、Registry、capability、EgressPolicy 和指纹，输出
`network_calls=0`。它不会检查账号能否调用模型。

## 真实合成文本 Smoke

真实 smoke 只使用内置公开短文本，必须显式设置总开关、对应细分授权所使用的 Profile，
并提供所需环境变量：

```bash
RAG_ALLOW_EXTERNAL_API=true \
JINA_API_KEY=... \
.venv/bin/python scripts/dev.py provider-smoke --provider jina

RAG_ALLOW_EXTERNAL_API=true \
DASHSCOPE_API_KEY=... \
ALIYUN_MODEL_STUDIO_WORKSPACE_ID=... \
ALIYUN_MODEL_STUDIO_REGION=cn-beijing \
.venv/bin/python scripts/dev.py provider-smoke --provider aliyun-qwen37
```

命令只输出 Provider、维度和调用数，不打印向量、Key 或完整响应。没有 Key 时不应运行
真实 smoke。

## 官方字段核对

2026-09-01 核对来源：

- [Jina v5 text Embedding API](https://jina.ai/news/jina-embeddings-v5-text-distilling-4b-quality-into-sub-1b-multilingual-embeddings/)
- [Jina Reranker API](https://jina.ai/reranker/)
- [阿里百炼文本向量同步 API](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api/)
- [阿里百炼地域与业务空间域名](https://help.aliyun.com/zh/model-studio/regions/)

官方 Schema 或账号可用性发生实质变化时，不得静默换模型；应按阶段决策门暂停。
