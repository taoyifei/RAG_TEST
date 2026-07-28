# 阻塞项

## 2026-07-28 新目标任务 0 基线不一致

- `artifacts/` 只读聚合 SHA256 命令退出 0，但当前值为
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`，
  与 `PROGRESS.md` 冻结基线
  `ee2ec74eb8cb39e7676ce66deae57e47525e6f69be818d567d40d711553a6415`
  不一致。当前目标禁止修改该目录；只继续只读定位差异，不恢复或删除内容。

## 本目标按边界保留的用户执行项

- 基础镜像下载：用户需按
  `design/public/offline-build-and-server-deployment.md` 拉取并核对三张
  固定 digest 基础镜像；本目标禁止代理下载。
- GPU 构建与离线双包：用户需执行 runtime wheel/OCR 资产准备、
  `docker buildx build --network none`、断网自检、双包生成与双层 SHA 校验；
  本目标禁止代理 build/save/package。
- 服务器冒烟与回滚：用户需通过 `${RAG_SERVER}` 上传到
  `/data/tyf/RAG/incoming`，完成安全解包、GPU OCR、Qdrant、`/live`、
  索引任务、备份和回滚实测；本目标禁止代理 SSH/SCP/部署。
- 生产验收：`deployment/config/retrieval.json` 仍为 `provisional`。用户需用
  人工冻结集确定参数，完成活动证据、质量门槛、10 万 chunk 和 5 并发
  30 分钟验收后才能使 `/ready` 返回 200。
- 上述均是明确的职责边界，不代表已获得真实 GPU、服务器或生产指标证据；
  代理未伪造对应输出。

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
- 解除条件：按
  `design/public/offline-build-and-server-deployment.md` 回填命令输出。

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

## P1：完整 chat-template token 预算尚未实现

- 状态：本轮任务书明确禁止实现完整 chat-template token 预算，现有预算仍以
  冻结 tokenizer 对问题、历史、证据和输出的分项上限为边界。
- 影响：当前单元测试只能证明各分项有界，不能证明生产 chat template 的全部
  控制 token、角色包装和服务端模板开销已被精确计入；不得把该结果表述为
  8192 上下文的完整预算证据。
- 解除条件：取得生产端实际 chat template/revision，在不改变回答 schema 和
  模型参数的前提下实现完整计数，并加入边界恰好通过/超一 token 拒绝的反测。
