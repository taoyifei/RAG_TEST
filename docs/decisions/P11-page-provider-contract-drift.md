# P11 页面 Provider 校验合同漂移

状态：`RESOLVED_IN_SOURCE_JINA_LIVE_VALIDATED_ALIYUN_STILL_BLOCKED`。

发现日期：2026-09-04。

## 发现

用户完成 Jina 与阿里云百炼页面配置并授权 Live 后，在第一次真实请求之前进行
静态合同复核，发现页面连接验证与正式百炼 Adapter 使用了两套不同协议：

- 页面验证使用 `https://dashscope.aliyuncs.com` 和
  `X-DashScope-WorkSpace`，请求体仍是 Jina 风格的顶层 `input`、`task`、
  `dimensions` 与 `region`；
- 正式 Adapter 与当前官方合同使用
  `https://{workspace}.cn-beijing.maas.aliyuncs.com`，请求体为
  `input.texts` 与 `parameters.text_type/dimension/output_type`，Query
  另外携带固定 `instruct`；
- 离线 Mock 同时兼容两种形状，导致错误页面合同仍能通过离线测试。

这属于任务书“真实模型 ID/API Schema 与官方当前合同不一致”的强制暂停条件。
因此没有用真实 Key 试错，也没有产生 Provider HTTP 请求或 Token 消耗。

当前参考：

- [阿里云百炼文本向量同步 API](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)；
- [阿里云百炼 Embedding 与 Rerank 模型](https://help.aliyun.com/zh/model-studio/embedding-rerank-model)；
- [Jina Embeddings v5 Text Small](https://jina.ai/en-US/models/jina-embeddings-v5-text-small/)；
- [Jina Reranker v3.5](https://jina.ai/news/jina-reranker-v3-5-faster-listwise-reranking-hybrid-attention-self-distillation/)

## 决策

1. 在合同修复、离线门禁和候选重建期间保持 Live 暂停；该阶段实际尝试为 0 次。
   后续首次 Jina 请求的 1 次 HTTP 与 19 估算输入 Token 单独记录在
   `P11-live-jina-network-error.md`。
2. 百炼页面验证改用 workspace 子域、`input.texts` 和
   `parameters.text_type=document|query`、`dimension=1024`、
   `output_type=dense`；Query 携带与正式 Adapter 相同的固定 `instruct`。
3. Jina 页面验证显式发送 `normalized=true`、`embedding_type=float` 与
   `truncate=false`，避免依赖服务默认值。
4. 百炼验证对 HTTP 状态、业务 `status_code/code` 与响应结构 Fail Closed，
   并把 `usage.total_tokens` 持久化为实际用量。
5. 页面验证使用统一向量校验，拒绝索引缺失、维度错误、非有限值与全零向量。
6. 离线 Mock 收紧为只接受各 Provider 的受支持请求合同，防止再次掩盖漂移。
7. 只有完整离线门禁和候选镜像重建、部署、revision identity 核对成功后，
   才恢复五项真实页面验证。

## 当前证据

工作树中的最小修复涉及：

- `src/rag_app/product/provider_runtime.py`；
- `tests/application/test_provider_runtime_registry.py`。

已执行：

```text
python -m pytest -q tests/application/test_provider_runtime_registry.py \
  tests/api/test_model_services.py tests/adapters/providers/test_aliyun_qwen37.py \
  tests/adapters/providers/test_jina.py
51 passed, 1 warning

python -m ruff check <两处变更文件>
All checks passed!

python -m ruff format --check <两处变更文件>
2 files already formatted

python scripts/dev.py check
compileall passed
ruff passed
mypy: Success: no issues found in 313 source files
missing_google_sections=0
1459 passed, 79 deselected, 4 warnings
```

完整离线门已经通过。正式候选镜像在 SHA
`88a260263db425e645a7ef759106342fa4b9d95f` 重建，manifest-list digest 为
`sha256:a7c7b3f08969a68613e25fde2d5267ef053b2ecf77ac54bd58c4e5cb28e1830f`，
原位部署后的 OCI revision 已核对一致。首次 Jina document validation 产生 1 次
真实 Provider HTTP、19 估算输入 Token，但在收到模型响应前以
`PROVIDER_NETWORK_ERROR` 失败。百炼修订后的真实合同因此尚未执行，不能将源码修复
或 Mock 合同测试标记为百炼 Live 成功。后续见
[P11-live-jina-network-error.md](P11-live-jina-network-error.md)。


## 2026-09-05 Live 复核

Jina 三项页面验证已取得真实成功响应。百炼 document 在 workspace 子域和原生
请求体下返回 HTTP 4xx；随后发现旧逻辑把所有百炼 HTTP 400 都误标为
`REGION_OR_WORKSPACE_INVALID`。

提交 `c4e68dfa1ba61fef8ac590e81b663682bb0c1a2b` 改为只解析 4 MiB 内 JSON
中的白名单业务 `code`，原始 `message` 与未知 `code` 均不持久化。定向门禁
46 项通过，完整离线门禁为 `1471 passed, 79 deselected`。新候选真实重试仍为
HTTP 4xx，但安全分类已变为 `PROVIDER_REQUEST_INVALID`。

endpoint、model、`input.texts`、`text_type=document`、`dimension=1024` 与
`output_type=dense` 均符合任务书和当前官方文档。真实服务仍拒绝请求，因此按任务书
暂停；不得删除 `text_type=document|query` 来绕过 C4，也不得用 Mock 宣称百炼
Ready。详见
[P11-live-aliyun-validation-error.md](P11-live-aliyun-validation-error.md)。
