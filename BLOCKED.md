# 阻塞项

## 语义路由真实校准与服务器回归

- 本轮按边界未访问 `.57/.58/.60`，没有调用真实 embedding 或 Qwen。因此
  `deployment/config/intent-router-calibration.json` 保持 `status=unverified`，
  `intent-router.json` 默认 `mode=shadow`；语义 profile 不会成为生产生效结果。
- 已提供 `evaluation/calibrate_intent_router.py` 与
  `evaluation/evaluate_intent_router.py`：前者只能以 tuning 集和当前配置的真实
  embedding endpoint 生成 verified 产物，后者以固定产物只报告 holdout 指标。实际
  校准必须记录 primary/secondary/slot macro F1、GENERAL fallback rate、coverage
  和全部身份 SHA；未经此步骤不得切换到 semantic/hybrid。
- app-only 更新后仍须重新执行 6 个自由问题，确认 6 个不同 trace ID、
  ANSWERED/PARTIAL、VALIDATION_OK、q4 不为 CONFLICT、q5/q6 无
  `CROSS_SOURCE_GROUP`、正常 `model_calls=1/retry_count=0`、exact cache 命中不再
  retrieve/rerank/llm.answer，且 4 个并发不同问题至少使用两个健康 Qwen endpoint。
  顺序请求持续选择最快副本不是失败；不得重建索引，需核验 index fingerprint 不变。
- 本地 Trace export 的实际 Qdrant 集成专项受 `127.0.0.1:6333` 返回 502 阻塞；
  不修改 Qdrant、索引或导出逻辑，待可用的本地/授权环境复跑。

## P09 远程质量边界

- P09 本地工程交付当前没有 Decision blocker。真实 Jina/Qwen dense-only Calibration、
  Remote Qdrant 和生产负载均未在本阶段调用或验证，因此
  `REMOTE_PRODUCTION_PROFILE_READY=false`；该边界不阻塞默认离线 API/SDK。
