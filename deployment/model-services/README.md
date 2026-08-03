# 共享 Embedding/Reranker 服务

这是显式选择 self-hosted 时才使用的独立可选工具。默认 RAG 部署复用已经通过
真实模型契约核验的 embedding、reranker 和四个 LLM；现有服务契约通过后不得
重复部署或额外占用 GPU。该目录、模型权重和模型镜像都不进入 RAG smoke 包。

该 Compose 只运行 `.60` 上共享的 Qwen3-Embedding-0.6B 和
Qwen3-Reranker-0.6B。它不 build、不 pull、不下载模型，也不属于某个 RAG
release 的生命周期。模型资产必须预先完整解包，两个固定镜像也必须预先加载。

先创建专用配置并按 `nvidia-smi` 的当次结果填写两个不同的空闲 GPU：

```bash
install -d -m 0700 /data/tyf/RAG/shared/env
install -m 0600 \
  /path/to/deployment/model-services/.env.example \
  /data/tyf/RAG/shared/env/model-services.env
```

启动前必须运行只读预检。预检会核对资产的总清单和子清单、模型 revision、
两张本地镜像、GPU/端口互斥以及 Compose 展开结果：

```bash
bash /path/to/deployment/model-services/preflight.sh \
  /data/tyf/RAG/shared/env/model-services.env
```

只有输出 `RAG_MODEL_SERVICES_PREFLIGHT_OK` 才能启动：

```bash
docker compose \
  --env-file /data/tyf/RAG/shared/env/model-services.env \
  -f /path/to/deployment/model-services/compose.yaml \
  up -d --no-build --pull never --wait --wait-timeout 300
```

在 RAG 的 `rag.env` 中只填写 origin，例如
`["http://10.242.180.60:8091"]` 和
`["http://10.242.180.60:8092"]`；不能追加 `/v1`、`/embeddings` 或
`/rerank`。服务启动后仍须执行项目的模型契约与 fleet 校验，Compose health
不能替代模型身份和推理结果校验。
