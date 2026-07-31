# 阻塞项

## P0：访问模式环境变量尚未映射进 Compose 容器

- 本轮已按硬要求把 `RuntimeSettings.access_mode` 设为必填且只接受
  `shared_corpus`，并在 `deployment/.env.example` 明确配置
  `RAG_ACCESS_MODE=shared_corpus`；缺失或 `permissioned` 的配置测试均会
  启动失败。
- `deployment/compose.yaml` 当前为 app/worker 显式枚举容器环境变量，没有
  传入 `RAG_ACCESS_MODE`。Compose 的 `--env-file` 只负责变量插值，不会把
  未引用的变量自动注入容器，因此按现状启动的 app/worker 会因缺少必填字段
  失败。
- 当前任务的“只允许”白名单列出 `deployment/config/pipeline.json`、
  `deployment/ASSETS.sha256` 和 `.env.example`，没有授权修改
  `deployment/compose.yaml`；本轮不能越界补两处映射，也不能假装离线部署
  已可启动。
- 解除条件：明确授权在 app 与 worker 的 `environment` 中各增加
  `RAG_ACCESS_MODE: ${RAG_ACCESS_MODE:?required}`，随后更新资产 SHA，并重跑
  两种 Compose、缺失值和 `permissioned` 的启动前反测。
- 2026-07-31 自动续跑第 1 次复核：实际解析默认与 `index` profile Compose
  JSON，样例变量命中数为 1，但 `rag-app` 的 30 个环境键和 `rag-worker`
  的 31 个环境键中均不存在 `RAG_ACCESS_MODE`。根目录只有
  `deployment/.env.example`；Dockerfile 只复制 deployment config/assets，
  不复制该样例，Compose 也没有 `env_file`。因此不存在白名单内可保持“显式
  必填”的替代注入路径；阻塞条件与首次发现完全相同。
- 2026-07-31 自动续跑第 2 次复核：使用 Docker Compose v5.1.2 分别解析默认
  与 `index` profile。默认配置的 app/worker 环境键数为 30/1，`index`
  profile 为 30/31，四处均没有 `RAG_ACCESS_MODE`。仓库排除 `.venv` 后唯一
  `.env*` 文件仍为 `deployment/.env.example`；`deployment/compose.yaml`
  SHA256 仍为
  `d7849a77e71c554614d6ddd8cd957da8a91ad7230e5fbaa57f5a673296ed3b5c`
  且相对 HEAD 无 diff。阻塞条件已连续 3 个 goal turn 相同，现有白名单内
  无法继续闭环。

## 历史审计：真实 Git index 原始字节 SHA 与旧任务 0 基线不同

- 任务 0 记录的 `.git/index` SHA256 为
  `dee80a74563a99d765fb3d34ce87860a6bf068a73ed20d7bfadcbd76d3be8b8f`；
  最终只读复核为
  `19f4940557b5294103c562db4909e7127f659496430e4b918d43294352779a9a`。
- 当前 `git diff --cached --quiet` 退出 0，`git write-tree` 与
  `HEAD^{tree}` 均为 `96df5fdd1c51f7be7482be4d1878766324ac4b12`，HEAD 仍为
  `49c34074a0553711bae4796aeb42da3916f31623`；因此 staged 内容和索引逻辑树
  未改变，差异只存在于 index 原始字节/缓存元数据层。
- 本轮发布扫描始终使用独立 `GIT_INDEX_FILE`，最终候选 236 个文件、
  violations=0，临时 index 已精确删除；没有对真实 index 执行 add/reset。
- 白名单收敛终审期间，只读 `git status` 的缓存刷新使原始字节 SHA 再变为
  `3533a1c76120079ee84c02d8703db657a73c1ff084441ed3b49620ddb6555ea6`；
  最终临时扫描前后均保持该值。`git write-tree` 与 `HEAD^{tree}` 仍同为
  `96df5fdd1c51f7be7482be4d1878766324ac4b12`，staged 仍为 0，因此阻塞性质
  未变：只有缺失的任务 0 原始 index 字节副本才能满足字面 SHA 条件。
- 任务 0 的原始 index 字节副本已不在文件系统中，SHA256 不能反推出原文件；
  为避免覆盖用户真实 Git index，本轮不执行 `read-tree`、复制或其他写回。
  当前发布口径只使用 staged=0、`git diff --cached --quiet` 和
  `git write-tree == HEAD^{tree}` 三项逻辑不变量；原始字节差异不再是 blocker。
- 2026-07-30：完整交付说明后，用户再次明确要求 commit 并 push，视为接受该
  原始字节差异并覆盖仅针对本轮 Agent 的 Git 提交禁令；该历史证据继续保留，
  但不再阻止本次提交与推送。
- 2026-07-30 本轮任务 0 复核：HEAD 为
  `caf5bdba83c149845bc1f0e48d1dc8f3491fbe1c`，`git diff --cached --quiet`
  退出 0，`git write-tree` 与 `HEAD^{tree}` 均为
  `d59ea6c35c3c8c7409300b851e6caf0f26497367`，staged=0。当前原始 index
  SHA256 为 `af72443dfe0be82e6cc731256459ed7d36d7fa518f2e997401f75ed2b6e689f2`，
  仅保留审计，不要求恢复。

## 2026-07-29 离线发布链任务 0：Windows Git UNC safe.directory 拒绝

- 命令（PowerShell UNC 工作目录）：`git status --porcelain=v1`；退出码 `1`。
- 原始摘要：`fatal: detected dubious ownership in repository at '<WSL UNC repository path>'`；Git 建议全局加入 `safe.directory`。本任务不修改全局 Git 配置，后续基线与验收改用 WSL 仓库内的只读 Git 命令。
- 影响：无工作区、Git index 或远端状态修改；该环境差异不阻塞任务 0 的其余只读基线。

## 已解决的任务边界偏差：`evaluation/metrics.py`

- 本目标一方面只列出 `evaluation/{active_state.py,evaluate.py,
  chunking_experiment.py,chunking_ablation.py}` 为功能改动白名单，未列出
  `evaluation/metrics.py`；另一方面又硬性要求删除 `TrustedActiveEvidence`、
  `_TRUST_MARKER` 和公开 verifier，并让生产评分直接消费同进程现场扫描结果。
- 基线 `evaluation/metrics.py` 直接导入、公开导出并在 `evaluate_results()` 中
  接收和检查 `TrustedActiveEvidence`。若保持该文件只读，删除可信包装后模块导入
  立即失败；若保留当前最小改动，则白名单按字面不再是零越界。
- 当前保留 18 行最小必要 diff：删除该类型/loader 的导入导出与 `isinstance`
  伪边界，改收现场 `ActiveEvidenceManifest`，并让 schema v2 的任一真实 locator
  可参与引用匹配。没有借此修改指标、阈值或冻结集。
- 2026-07-29 Query Trace v1 任务书明确批准上一轮 18 行最小必要改动；
  该偏差不再是产品 P0，也不需要恢复。历史原因和最小 diff 仍保留在此供审计。

## P0：retrieval chunking 消融缺少真实模型与 tuning 文档键映射

- structural 四候选已在真实 6 DOCX 上完成且硬结构门槛全绿；这不能替代真实
  embedding/reranker 的 tuning 检索结果，也不能证明准确率提高。
- 当前任务禁止联网、访问 `.57/.58/.60`，生产 embedding/reranker revision 仍未
  核验；因此未运行 retrieval mode、未读取 holdout 标签，pipeline 保持
  `section-pack-v2-provisional`，`retrieval.json` 保持 `provisional`。
- 用户需先准备一个仅含冻结集 `documents` 映射、不含问题或 expected 的
  `tuning-document-map.json`，再在已核验模型环境执行：
  `.venv/bin/python evaluation/chunking_ablation.py docs --mode retrieval
  --tokenizer deployment/assets/tokenizers/embedding/tokenizer.json
  --pipeline deployment/config/pipeline.json
  --corpus-policy deployment/config/corpus-policy.json
  --retrieval-config deployment/config/retrieval.json
  --dataset evaluation/frozen/questions.json
  --document-map tuning-document-map.json --qdrant-url "$RAG_QDRANT_URL"
  --embedding-endpoint "$RAG_EMBEDDING_URL"
  --reranker-endpoint "$RAG_RERANKER_URL"`。
- 所需证据：四个独立临时 collection 的清理记录、模型 revision、候选总体及
  cross_chunk/table/numeric 的 Recall@5/10/20、MRR、rerank Recall@5；定参只看
  tuning，最终 holdout 另行一次性验收。

## 历史审计：2026-07-29 恢复检查发生一次联网读取

- 新任务书要求本轮不联网；恢复上一条“推送到新仓库”请求时，读取任务书和
  Git 状态被并行执行，其中
  `env GIT_TERMINAL_PROMPT=0 git ls-remote origin` 已实际联网并退出 0，
  返回 `HEAD` 与 `refs/heads/main` 均为
  `4fe7b26164e6ad1ee6b1f8477beed0473f7d49fe`。
- 当前本地 `main` 已跟踪 `origin/main`，远端提交发生在上一目标开始前或恢复
  边界；未执行 push。Query Trace v1 任务书明确要求该事实继续保留审计，但不再
  作为产品阻塞；本轮没有再次联网。

## 2026-07-28 新目标任务 0 基线不一致

- `artifacts/` 只读聚合 SHA256 命令退出 0，但当前值为
  `220473c637bc5179f2019948cc225dfb8130dd3cb928a6d71c82b6736f874c24`，
  与 `PROGRESS.md` 冻结基线
  `ee2ec74eb8cb39e7676ce66deae57e47525e6f69be818d567d40d711553a6415`
  不一致。当前目标禁止修改该目录；只继续只读定位差异，不恢复或删除内容。

## P1：Word 自动编号尚未渲染为可引用文本

- 2026-07-29 只读审计检测到 268 个 `list_level` 非空段落；当前
  `docx-parser-v3` 只读取段落 runs，不解析 `numbering.xml` 并渲染 Word 自动编号，
  因而 268 个自动编号 marker 均未作为可验证原文进入 text/source span。
- 本轮硬约束要求保持 parser revision 和解析行为不变，且禁止猜测或伪造编号文本；
  section-aware chunking 仅保留这些段落的原始 run 文本和列表层级，用换行组织连续列表项。
- 解除条件：实现并审计只读 Word numbering renderer，覆盖多级编号、restart、style
  继承和缺失定义的反测；更新 parser revision 后通过真实 6 DOCX 覆盖与引用核验。

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

## P0：真实 Qwen Prompt 与模型 HTTP 契约仍待目标网络执行

- 本轮只新增只读 `scripts/verify_model_contracts.py` 和 MockTransport 测试；
  任务边界禁止访问 `.57/.58/.60`，因此尚无真实 embedding、reranker 或
  四个 LLM 端点的通过报告，也没有据此填写任何 revision。
- 用户需在可达目标网络的环境分别执行下列命令；令牌只通过变量名传入脚本，
  不要把令牌值写进命令参数或报告：

  ```bash
  .venv/bin/python scripts/verify_model_contracts.py embedding \
    --endpoint "${RAG_EMBEDDING_URL}" \
    --model Qwen3-Embedding-0.6B \
    --expected-revision "${RAG_EMBEDDING_EXPECTED_REVISION}" \
    --token-env RAG_EMBEDDING_API_TOKEN \
    --dimension 1024

  .venv/bin/python scripts/verify_model_contracts.py reranker \
    --endpoint "${RAG_RERANKER_URL}" \
    --model Qwen3-Reranker-0.6B \
    --expected-revision "${RAG_RERANKER_EXPECTED_REVISION}" \
    --token-env RAG_RERANKER_API_TOKEN

  .venv/bin/python scripts/verify_model_contracts.py llm \
    --endpoint "${RAG_LLM_URL}" \
    --model Qwen/Qwen3-8B-AWQ \
    --expected-revision "${RAG_LLM_EXPECTED_REVISION}" \
    --token-env RAG_LLM_API_TOKEN \
    --context-limit 8192
  ```

- 端点不鉴权时必须省略 `--token-env`；设置该选项但环境变量为空同样不会
  发送 `Authorization`。`--expected-revision` 必须来自独立部署记录，不能
  使用 `unknown`、`main` 或 `latest`。若 health/models 均不返回 revision，
  还必须传 `--deployment-manifest <path>`；manifest 必须为非符号链接的
  只读文件，包含并以规范化 SHA256 绑定 endpoint、model、model/tokenizer/
  code revision、vLLM、quantization、max context 和 chat-template SHA。
- LLM 命令需对四个目标 URL 分别执行。每份 JSON 必须为 `status=passed`，
  model ID 和 endpoint revision 明确，rewrite/answer 都以严格 JSON Schema
  完成，finish_reason=stop、temperature=0、thinking=false；最大初次回答和
  最大 repair 请求都必须满足服务返回的
  `prompt_tokens + max_output_tokens <= context_limit`，且三项 usage token
  计数一致。embedding 还需 count/index/dimension/finite 全绿；reranker
  还需 count/index/[0,1] 全绿。
- 任一端点返回 `REVISION_MISSING`、model/schema/维度/索引/分数错误、截断或
  endpoint failure，均继续阻塞对应依赖。真实结果齐全前
  `deployment/config/retrieval.json` 继续为 `provisional`，pipeline 中
  embedding/reranker/LLM revision 继续为 `pending-server-verification`。

## 已解除：GitHub 远端 refs 可读取

- 2026-07-29 现场确认 `origin` 为
  `https://github.com/taoyifei/RAG_TEST.git`，本地 `main` 跟踪
  `origin/main`；远端 `HEAD` 与 `refs/heads/main` 均指向
  `4fe7b26164e6ad1ee6b1f8477beed0473f7d49fe`。
- 该读取同时构成本轮禁止联网边界的偏差，已在本文件置顶单独保留；后续不再
  在线复核远端。

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
