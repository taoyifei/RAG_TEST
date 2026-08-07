# 阻塞项

## Industry 仍需人工或服务器现场完成

- LibreOffice Writer 阻塞已解除。用户授权后固定 Ubuntu Jammy Writer package 只安装
  到本地 converter image；GM-01～GM-10 的真实转换、清洗、Parser audit 和确定性复跑
  已通过。`封面.doc` 与 `管理制度清单.doc` 是用户主动删除的无用文件，不是缺失项。
- GM-01 的图像文字可被 Parser 识别，但组织机构图的层级与连线仍需人工对照原件复核；
  当前 corpus audit 保留 `MANUAL_STRUCTURE_REVIEW_REQUIRED`。
- 受限转换用户不能在容器内二次创建 `unshare -n` namespace，公开 audit 因此保留
  `NETWORK_NAMESPACE_UNAVAILABLE`。实际构建和转换由外层 Docker `--network none`
  执行，但不把该事实改写成进程内 namespace 已启用。
- 用户提供的服务器清单已确认 `.60` 有可复用的 OCR 镜像和当前
  `qdrant/qdrant:v1.18.3`，但轻量包会在现场再次核对完整 image ID、平台与
  revision，任一不一致即阻塞。目标服务器的 Industry 路径、未占用 OCR GPU、
  内存/磁盘和模型 endpoint 仍需现场确认；首次部署、Industry full index、真实问题
  retrieval tuning/holdout、备份、回滚、SLA 与生产安全验收均未在本地执行。

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
