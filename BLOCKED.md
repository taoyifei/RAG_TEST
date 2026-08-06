# 阻塞项

## 0. 自由问题服务器回归与实际性能

- 本地已用 6 个固定自由问法完成意图、逐 claim 来源隔离、SOURCE_SEPARATED、Trace
  manifest 和支持质量诊断专项；但本轮没有访问或更新 `.60`，也没有调用真实 Qwen
  endpoint。因此 6 个新 trace 的 ANSWERED/PARTIAL、VALIDATION_OK、model_calls=1、
  retry_count=0、q5/q6 无 `CROSS_SOURCE_GROUP`、cache 命中、4 并发副本分布和首个
  claim <3 秒，仍须在 app-only 更新后按任务书验收。
- 解除条件：仅更新 `rag-app` 后重新执行 6 问，导出含
  `TRACE_EXPORT_MANIFEST.json` 的 ZIP，核验 6 个不同 trace ID、manifest exact set、
  index fingerprint 不变；顺序请求持续选择最快 `.58:8001` 不作为失败。

本文件只保留当前目标明确要求继续保留的七类外部阻塞。已解决环境、测试和安全
解包问题均已移至 `PROGRESS.md`。

## 1. 真实模型契约

- 当前任务禁止访问 `.57/.58/.60`，因此没有 embedding、reranker 和四个 LLM
  的真实 health/models、revision、schema、维度、上下文上限与最小请求报告。
- `deployment/config/pipeline.json` 中生产模型 revision 继续保持
  `pending-server-verification`；smoke 包只携带模型契约验证器，不伪造报告。
- 解除条件：在目标网络对六个端点分别运行
  `scripts/verify_model_contracts.py`，每份脱敏 JSON 均为 `status=passed`，且由
  独立部署记录绑定 endpoint、model/tokenizer/code revision 与服务版本。

## 2. retrieval 定参与冻结

- `deployment/config/retrieval.json` 继续为 `provisional`，没有使用真实模型与
  人工冻结集完成 tuning/holdout，因此不能改为 frozen 或使生产 ready。
- 当前配置 SHA256 为
  `ee0a6356a6939635f7f7da433198283e7f6592c8ea6067cd6b5ae3ec68b92539`；配置完整性
  测试已改为从 `deployment/ASSETS.sha256` 读取唯一摘要并核对实际文件，继续
  断言 `status=provisional` 与 `freeze_decision_sha256=null`。该修复只消除重复
  摘要来源，不代表 retrieval 已完成定参或冻结。
- 解除条件：核验真实模型契约后，用 tuning 集确定参数、独立 holdout 验收并生成
  `FREEZE_DECISION`；届时由获授权任务原子更新配置及其冻结契约。

## 3. GPU OCR 实测

- 当前 HEAD 的 OCR 镜像已在本地以 `--network none` 实际构建、自检并验证为
  `linux/amd64`，revision 精确等于
  `d7d2546f51d912be0cb0025757922d770f05d833`。
- 本任务禁止访问服务器，因而没有 `.60` NVIDIA runtime、GPU index/显存、
  `/ready`、真实图片请求、耗时、OOM 或重启证据；本地镜像成功不能替代 GPU 验收。
- 解除条件：服务器只读 preflight 通过后，在获授权窗口完成 GPU OCR 启动、
  真实请求、资源与故障恢复验收，并保留脱敏输出。

## 4. EMF 转换器

- 18 个 EMF 引用尚无经过许可证、安全和离线分发审计的固定转换器资产；当前实现
  必须明确返回 `EMF_RASTERIZER_UNAVAILABLE`，不得猜测或静默计为 OCR 成功。
- 解除条件：选择固定版本，冻结二进制 SHA256/许可证和命令契约，完成畸形输入、
  文件/CPU/内存/超时上限反测后再更新 parser/OCR revision。

## 5. Word 自动编号

- 真实 DOCX 中有 268 个 `list_level` 非空段落；当前 parser 读取 runs 与列表层级，
  不解析 `numbering.xml` 渲染自动编号，编号 marker 不能作为可验证引用原文。
- 本任务禁止修改 Parser；不能用猜测编号填充证据。
- 解除条件：另行实现只读 numbering renderer，覆盖多级编号、restart、style 继承
  和缺失定义反测，更新 parser revision 后重建索引并复核 6 个 DOCX。

## 6. production 验收

- 本轮只完成本地 smoke：app/OCR 实际构建、断网自检、Qdrant 归档、13.34GB
  runtime 与 21.95MB corpus 双包、七文件 sidecar 和全新目录解包验证均通过。
- 未 SSH/SCP、未访问或部署 `.60`，也没有服务器 `docker load`、GPU、真实模型、
  6/6 入库、50 题指标、10 万 chunk、5 并发、备份/回滚与生产 ready 证据。
- 解除条件：前五类依赖满足后，在获授权服务器窗口执行 SHA 校验、离线 load、
  `compose up --no-build --pull never`、全新卷启动、完整质量/性能/故障与回滚验收。

## 7. support-id 回答、缓存与性能服务器验收

- 本地已把模型回答改为只选择原子 `support_id`，最终 quote/locator 由应用从
  source span 确定性恢复；同时增加 Answerability Gate、来源关系隔离、部分回答、
  高置信抽取兜底、精确 SQLite 缓存、同键 singleflight 和四副本负载调度。索引
  fingerprint 仍为 `sha256:dd16e57d...`，不需要重新索引。
- 当前任务禁止访问服务器，因此还没有 `.60` 更新后的 9 问回归、exact cache
  `<200ms`、非缓存回答 p50/p95、四副本并发分布，以及更新前后 alias、manifest、
  index fingerprint 与 Qdrant 点数不变的现场证据。不能把本地模拟延迟描述为真实
  Qwen 性能。
- 解除条件：经 `.54` 转传 app-only 三文件包并只更新 `rag-app`。7 个可回答问题
  必须 answered/partial，火星基地 `RAG-999/2099` 必须 NOT_FOUND 且
  `model_calls=0`；重复独立问题必须命中 exact cache 且不调用 embedding、reranker、
  LLM；正常请求只使用一个 Qwen，4 个并发不同问题应分布到多个副本。非缓存回答
  目标 p50 `<=10s`、p95 `<=15s`，检索到证据组装继续 `<2s`。
- 若完成 app-only 更新后非缓存回答仍超过 15 秒，只做 A/B 取证建议，不直接修改
  模型服务：核对 vLLM Automatic Prefix Caching；单 endpoint 对比 n-gram/suffix
  speculative decoding；对比 TP 配置；分别记录 queue 与 decode 指标。

## 8. 已校验 claim 流式回答服务器验收

- 本地已实现严格 vLLM SSE、增量 claims-only 解析、逐 claim 引用门禁、NDJSON
  `answer_start/claim/answer_progress`、前端去重与 AbortController 取消传播；本地专项
  与静态门禁均已通过，index fingerprint 不变。
- 当前任务只允许生成 app-only 更新包，未访问或更新 `.60`，因此尚无反向代理实际
  不缓冲、首条合法 claim 到达时间、浏览器中途取消后上游 in-flight 立即释放，以及
  取消请求不写 cache/conversation 的服务器现场证据。
- 解除条件：经 `.54` 转传并只更新 `rag-app` 后，确认响应头包含
  `Cache-Control: no-store, no-transform` 和 `X-Accel-Buffering: no`；可回答问题的
  `claim` 事件早于 `final` 且页面不重复引用；中途取消后 Trace 为
  `stream_cancelled=true`，没有对应 cache/conversation 写入；活动 alias、manifest、
  index fingerprint 与 Qdrant 点数保持不变。
