# 阻塞项

## P1：GitHub 远端 refs 当前无法复核

- 状态：只阻塞最终“远端 refs 不变”的在线复核；不阻塞本地测试和分组提交。
- 证据：2026-07-27 执行
  `git ls-remote https://github.com/taoyifei/RAG_TEST.git`，60 秒无输出后
  超时退出 124。
- 已遵守边界：仓库仍无 remote；未认证、未 push。
- 解除条件：网络恢复后只读执行同一命令并与“无 refs”基线比较。

## P0：OCR GPU 镜像构建和服务器实测由用户执行

- 状态：代码、固定模型、CPython 3.10 wheelhouse、Dockerfile、Compose 和手册
  已就绪；本任务明确禁止代理执行 `docker build/save`、上传、SSH 或访问
  `.57/.58/.60`，因此不能冒充 GPU/离线部署验收。
- 本地已证实：PaddleOCR 3.5.0 + PaddlePaddle 3.3.0 CPU 对一张真实 DOCX
  内 PNG 成功识别 51 行、189 个非空白字符，均值置信度 0.943705；
  这仅是模型和代码接缝冒烟，不代表服务器 GPU 指标。
- 待用户证据：断网构建退出 0、镜像架构/digest、`--network none` 自检、
  GPU `ready`/真实请求、显存和耗时、离线 save/load/up/rollback。
- 解除条件：按 `design/public/paddleocr-offline-deployment.md` 回填命令输出。

## P0：EMF 转换器资产尚未冻结

- 状态：无 shell、临时目录、CPU/内存/文件/输出/超时限制的
  `EmfRasterizer` 接口与失败状态已实现；镜像没有擅自安装未选定转换器。
- 影响：18 个 EMF 引用在未提供经过许可证与安全审计的固定转换器前会明确返回
  `EMF_RASTERIZER_UNAVAILABLE`，不能计作 OCR 成功。
- 解除条件：选择可离线分发的固定转换器版本，记录二进制 SHA256/许可证，
  适配固定 `input.emf output.png` 命令后做畸形文件与资源上限实测。

## P0：生产模型与冻结检索参数仍需环境证据

- 状态：当前 `deployment/config/retrieval.json` 仍明确为 `provisional`，
  readiness 会拒绝把它当成生产冻结参数。
- 影响：阻塞真实 6 文档最终入库、活动 alias、50+ 题指标和 5 并发性能验收；
  不阻塞本目标的源码安全、P0 评测逻辑和 OCR 部署资料交付。
- 解除条件：用户在目标网络核验 embedding/reranker/LLM revision、schema、
  维度与上限后，以人工冻结集定参并执行完整验收。
