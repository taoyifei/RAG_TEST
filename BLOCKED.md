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
- `.60` 现场已确认可复用 OCR 镜像；Qdrant 的 tag、运行容器和容器内版本一致为
  `qdrant/qdrant:v1.18.3`，实际 `linux/amd64` image ID 为
  `sha256:ecc81d662bb9bb734db879b94461eb44be38604fc259491d478ad7e673238a0d`。
  轻量包必须固定该服务器身份，并在现场再次核对完整 image ID、平台与 revision；
  任一不一致即阻塞。GPU 3 在 2026-08-07 现场快照中无计算进程，但正式预检仍须
  重新确认。旧 release `732204a5555b-87860c8b7496` 因 pipeline/corpus policy
  语义 SHA256 绑定错误而在 app 启动时 fail closed。修正版
  `b6274a1458c3-87860c8b7496` 已成功启动；首次 full index 的原 job 已完成 10 个
  source 并发布 Industry collection。builder v5 已从既有 manifest 快照和
  succeeded job 续跑完成最终 report，当前 `/ready=true`，不得重跑 worker。
  旧 release 将全部 `verified` point 与只允许 `official` 的 retrieval 配置组合，
  曾导致正向 smoke 在元数据预过滤后零候选；builder v6 已部署且 `/ready=true`，该
  阻塞已解除，也没有重新索引。当前 20 问诊断剩余的是 smoke 问题过宽和模型把通用
  制度改名为培训专属主体：本地已增加来源特定问题与
  `UNSUPPORTED_QUESTION_ANCHOR` 门禁。`70faf374acaa` 已由用户在 `.60` 无重索引
  部署且 `/ready=true`，但 005 的真实 Trace 显示模型首次与修复均未引用指定来源，
  旧 fallback 又在显式主体过滤前截断候选，最终为 `VALIDATION_FAILED`。本地已用
  通用 source-label/正文锚点顺序修复并保持 index fingerprint 不变；本地轻量包已从
  唯一 clean commit 重建并通过 package selfcheck，仍需由用户在 `.60` 无重索引更新
  并复跑 20 问。真实问题回归、备份、回滚、SLA 与生产安全验收仍未完成。

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
