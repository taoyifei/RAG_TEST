# 阻塞项

## CURRENT STATUS：2026-08-11 e584 二次复核缺口与本地代码门禁已收口

旧 `2c4cf220...` 的 env-only last-good、source/target/absent pointer 恢复、
`verifying/validated/verified` 崩溃窗口、Trace WAL 并发写入与跨 update ID 全局锁均保留。
本轮又补齐 post-verified 人工撤回的 source pointer/共享锁，以及
`prechecking/activating/activated` 在 env rename、状态落盘和 App recreate 之间的硬中断
恢复。updater 脚本专项为 `58 passed in 195.07s`，非 Qdrant 扩大矩阵为
`247 passed, 1 warning`，固定 Qdrant 关联集为 `174 passed, 59 warnings`，model/OCR
合同为 `158 passed, 1 warning`，单次全量为 `1261 passed, 61 warnings`，均 0 failed。
静态、Compose 和源码 asset 也已通过；当前仅剩从本地 clean commit 构建并 fresh 验证
最终 8 文件包，不需要服务器信息。

1. **服务器验收尚未执行。** 本轮没有访问任何实际服务器，没有部署，也没有现场运行
   Industry 20 smoke、UI/Trace 验收或核对容器、alias、manifest、point/source count。
   本地 clean-commit 构建和 fresh selfcheck 只能支持后续内网 canary，不能证明现网已
   更新。上传、执行 updater 和现场验收必须等待用户后续单独授权。
2. **当前运行等级仍是 demo/canary。** `RAG_RUN_MODE=demo`、retrieval 为
   `provisional`、intent router 为 `shadow`、calibration 为 `unverified`。即使本地
   `1261 passed`，也不能宣称 production ready；production 仍需正式校准、冻结和服务
   器 acceptance。
3. **HTTP demo 是明确接受的内网风险。** 非 Secure UI Cookie 仅在五项显式配置同时
   满足时允许，production 已 fail closed，但 HTTP 本身不提供链路加密；普通问答访问
   边界仍依赖内网 ACL、反向代理或企业 SSO。Trace/Admin 继续只接受独立 Admin Token。
4. **Trace 明文属于受控敏感数据。** 7 天独立上限与 Trace TTL 取较小值，到期只清空
   `question_text`。正文禁止进入普通日志、Stage NDJSON、Header、ZIP 文件名/manifest、
   缓存键和 release manifest；服务器上的备份权限、访问审计、到期清空和日志反查仍需
   canary 现场验收。
5. **遗留制品全部禁止使用。** `artifacts/industry-app-update/809fb71f5e50` 与
   `artifacts/industry-serving-update/d5c03cf9b97e`、`8755bf379c8f`、`195f9aca2c63`、
   `e5844e531c45`
   均永久失效，不得上传、部署或作为验收证据。最终包必须由本轮新的 clean commit 构建
   到独立 SHA12 目录，顶层仍为 8 文件、runtime 为 19 文件，且
   `reindex_required=false`、index fingerprint 不变。
6. **仓库历史命中不等于交付泄漏。** 历史状态文档、测试内网地址和合成 secret fixture
   不能靠删除历史事实制造绿灯。硬门禁针对实际 8 文件 delivery、19 文件 runtime 与
   fresh extraction；交付物不得包含真实 IP、个人路径、secret、问题正文或业务 corpus。

identity-bearing commit 不记录自身 SHA 或构建后制品摘要；最终 revision、包路径、逐文件
SHA256、canonical digest、镜像内 asset-selfcheck 和 fresh extraction 结果由构建后交付
报告给出。后续服务器更新必须从 fresh shell 执行，不 source private env，不运行
`run-index.sh`，不重建 corpus，不重启 worker/OCR/Qdrant；只有现场 acceptance 后才能称
为内网 canary 上线。

## ARCHIVE：2026-08-10 本地实现暂停快照

本节仅为当时的暂停历史，不再是当前活动入口。用户随后已提供并批准 UI Cookie、Trace
明文保留和 serving update 的继续执行方案；其中未完成、未验证的陈述只说明当时状态，
不得覆盖文首 CURRENT STATUS 或本轮更新后的 `PROGRESS.md`/`INCIDENTS.md`。

### Git 与工作区状态

- 当前分支为 `Industry`，提交基线仍为
  `cd5e37765b16b6ae9ae46a15f32e669aec652e38`；`main` 仍为
  `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`。任务开始时
  `origin/Industry` 与该基线一致，Industry 相对 main 为 ahead 4、behind 0。
- 工作区当前不干净，且没有形成可发布提交：25 个已跟踪文件被修改，9 个文件未跟踪，
  共 34 个路径。已跟踪 diff 为 `880 insertions, 118 deletions`；新增文件尚未计入该
  diff 统计。
- 已跟踪修改涉及 BFF/UI、Trace schema/导出、显式来源锚点、运行时身份命令、两套
  Compose/env 和对应测试：
  `deployment/config/pipeline.json`、`deployment/{industry,simple}/.env.example`、
  `deployment/{industry,simple}/compose.yaml`、`frontend/{app.js,debug.html,debug.js,index.html}`、
  `src/rag_app/api/app.py`、`src/rag_app/cli.py`、
  `src/rag_app/generation/{answer.py,evidence.py}`、`src/rag_app/model_contracts.py`、
  `src/rag_app/query_service.py`、`src/rag_app/runtime.py`、`src/rag_app/settings.py`、
  `src/rag_app/tracing/{__init__.py,models.py,recorder.py,store.py}`，以及 4 个既有测试文件。
- 未跟踪文件为 `deployment/industry/update-app.sh`、
  `scripts/build_industry_app_update.py`、`src/rag_app/api/ui_session.py`、
  `frontend/{index-bearer.html,app-bearer.js}` 和 4 个新增专项测试文件。它们只是本地候选，
  不能当成已提交、已构建或已部署能力。
- 本轮没有修改 main、没有 push、没有访问服务器、没有部署、没有构建镜像或发布包，
  也没有运行索引、转换 corpus 或重启 worker/OCR/Qdrant。

### 已有但仅限局部的实现与验证证据

- 本地候选已经搭出以下方向：普通页面的 same-origin UI session/CSRF 路由；保留
  `/api/chat` Bearer 合同和独立 Admin Token；可配置 `hash_only|plaintext` 的 Trace
  问题采集与 v1→v2 SQLite migration；Trace preview/detail/export；来源编号边界；
  `rag-app runtime-state`；Industry app-only builder 和 updater 草案。
- 首次新增红测曾因缺少 `UiQueryAuthMode`、`TraceQuestionCapture` 和
  `scripts.build_industry_app_update` 在 collection 阶段失败。这是实现前的真实红灯，
  不是伪造失败。
- 后续 UI/Trace/anchor 核心小集合曾得到 `14 passed, 1 warning in 1.97s`；Industry
  builder 单测曾得到 `1 passed in 0.74s`。这些结果只证明对应局部，不覆盖完整任务。
- 2026-08-10 暂停前重新执行新增 UI session、Trace question、Industry builder、
  Industry updater 和 asset selfcheck 测试，真实结果为
  `12 passed, 2 failed, 1 warning in 2.97s`。两个失败均来自
  `tests/test_industry_app_update_script.py`：
  `test_update_is_hermetic_force_recreates_only_app_and_preserves_index` 和
  `test_failed_new_identity_restores_and_reverifies_old_app`。
- 两个 updater 用例都在更新前以“旧 app index fingerprint 与更新包不一致”退出，
  尚未进入新 app force-recreate 或回滚验证。直接触发点是 updater 用 `sed` 匹配紧凑
  JSON，而测试替身输出的合法 JSON 在冒号后含空格，导致 fingerprint 解析为空。即使
  当前 `rag-app asset-selfcheck` 恰好输出紧凑 JSON，这种字符串解析仍是脆弱接口；后续
  应决定用 JSON parser 修复脚本，或至少建立明确且受测的输出合同，不能把两条失败改写
  成 updater 已通过。
- `bash -n deployment/industry/update-app.sh` 当前通过；6 个新增 Python 文件通过只读
  AST 解析。`git diff --check` 当前通过。这些只属于语法/空白检查，不证明运行语义。
- 修改前记录的 index fingerprint 为
  `sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a`，
  serving fingerprint 为
  `sha256:41dc694db23d1895b08a703e058fc5ea6d7511da9484e42268c6bb3258c81c9b`。
  当前尚未完成最终资产装配和前后 fingerprint 门禁，不能声明修改后 identity 已冻结。

### 当前活动阻塞与风险

1. **Industry updater 仍是红灯。** 成功更新路径、失败回滚路径、旧状态完整复验都没有被
   当前行为测试真正执行；`deployment/industry/update-app.sh` 的 427 行候选脚本不可用于
   服务器。
2. **资产清单已漂移。** 直接运行本地 `rag-app asset-selfcheck` 失败，准确错误为
   `资源 SHA256 不一致：deployment/config/pipeline.json`。当前不能构建可信 app image；
   `deployment/ASSETS.sha256` 必须在最终 prompt/pipeline 决定后再更新并复验，不能为让
   测试通过而提前掩盖中间状态。
3. **Compose 环境隔离未覆盖完整部署链。** 目前只在新增 updater 草案中尝试 hermetic
   Compose；`deployment/simple` 与 `deployment/industry` 的 preflight/deploy/index/
   verify/rollback 入口尚未全部改造和污染环境反测。历史的 `RAG_PORT=8188` 覆盖
   training 8088 风险仍存在。
4. **last-good 两阶段晋升未实现。** 现有 Industry deploy/index/verify 脚本没有形成
   `candidate -> deployed -> indexed -> verified -> last_good` 状态机；deploy 成功但
   index 或 smoke 失败时仍缺少可靠的不晋升证据，中断恢复和 rollback 恢复上一
   last-good 也未覆盖。
5. **OCR ownership-aware preflight 未实现。** `deployment/industry/preflight.sh` 本轮
   没有修改，空闲 GPU、当前受管 Industry OCR、未知 PID、training OCR、镜像/revision
   不匹配和 external OCR 六类矩阵均未验证；历史自占用误报仍可能复现。
6. **desired/observed 终态门禁只有草案。** 新 updater 代码虽尝试检查 env、Compose、
   image ID、OCI/wheel revision、8188、live/ready、alias/manifest/point、mount 和其他
   容器 identity，但 updater 红测在前置 fingerprint 阶段就中止，因此这些检查尚无
   行为证据，也未成为所有部署入口共享的受测能力。
7. **BFF 安全测试矩阵不完整。** 当前有 token-free 资产、Cookie 属性、CSRF、Origin、
   伪造 Cookie、Admin 隔离和 `/api/chat` Bearer 兼容的局部测试；仍缺独立的 session
   expiry、HTTPS Secure Cookie、取消传播、429/503、stream incomplete、响应/日志全局
   secret 扫描和反向代理 Host/scheme 边界证据。Industry 内网 HTTP 显式关闭 Secure
   的风险也尚未写入运维文档。
8. **Trace plaintext 验证矩阵不完整。** 当前有 hash-only、plaintext、v1→v2 幂等迁移
   和 partial schema fail-closed 的局部测试；仍需完整证明 list preview、detail、单 JSON、
   ZIP manifest/文件名、XSS `textContent`、prune、普通日志不落正文，以及 SAFE/
   DIAGNOSTIC/FULL 与 capture 配置组合。明文沿用 Trace TTL 的保留与备份敏感数据策略
   也尚未进入文档。
9. **来源锚点只完成局部合同测试。** GM-03/GM-030、多个编号整体覆盖和 multi-anchor
   commit barrier 的候选测试/修改尚未经过任务要求的完整专项、Industry 20 smoke 合同
   回归或真实服务器问题验证；不能据此宣称现网回答行为已改善。
10. **完整门禁均未完成。** 尚未完成 compileall、Ruff、strict mypy、Google docstring、
    `node tests/frontend_stream_test.mjs`、两套 shell `bash -n`、两套 Compose config、
    release safety、package selfcheck、既有 app-update/Industry/Trace/Query 合同测试或可行时
    的全量 pytest。当前只知道新专项仍有 2 个确定失败。
11. **文档信息架构未完成。** `PROGRESS.md` 尚未增加本目标 CURRENT STATUS，
    `BLOCKED.md` 仍保留较长历史事故，`INCIDENTS.md` 尚未创建；本节仅先建立不误判的
    当前入口，未重写历史事实。
12. **没有提交和制品。** 当前无本地 commit，无
    `artifacts/industry-app-update/<sha12>/`，无四文件 exact set、各文件 SHA256、fresh
    extraction/selfcheck 或 clean-build 证据；`reindex_required=false` 也没有最终包级
    证明。

### 后续继续前是否需要额外信息

- 定位当前两个 updater 红灯不需要服务器信息，也不需要用户提供新日志；本地测试已经
  把失败收敛到 fingerprint JSON 解析边界。修复方式应在用户重新授权代码修改后决定。
- 完成本地实现也不需要登录 `.54/.57/.58/.60`。只有后续真实部署验收才需要在全新
  shell 记录更新前后的 app/OCR/Qdrant 容器 identity、alias、manifest SHA、point count、
  mount、端口和 live/ready；本轮禁止获取这些数据。
- 仍需用户最终接受两项明确策略：Industry 为内网 HTTP 时是否接受
  `Secure=false` 的短期 UI Cookie 风险，或要求先提供 HTTPS/反向代理；以及问题明文是
  仅沿用现有 Trace TTL，还是增加独立且更短的 plaintext retention。两项选择不会改变
  `/api/chat` Bearer 与 Admin Token 必须继续独立的边界。
- 根据用户最近贴出的现场输出，training 8088 与 Industry 8188 当时均为 healthy，
  Industry 20 smoke 当时为 `smoke_passed=20`。这是用户提供的历史现场证据，本轮未重新
  访问服务器，可能随外部状态变化；不能作为当前本地候选已部署或已验收的证明。

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
  通用 source-label/正文锚点顺序修复并保持 index fingerprint 不变；随后部署的
  `5ce587010422` 在 008 暴露第二个真实缺口：GM-03 已以 rerank rank 6、score
  0.9727 进入 Evidence，但旧门禁允许 GM-04 用重合标题替代精确编号。现已用通用的
  “精确编号优先、多个编号整体覆盖”规则修复，未硬编码工业制度或固定答案；
  `2c4cf220c7cf` 已在 `.60` 仅重建 app 容器并通过 20 问，Industry answer 阻塞解除。
  training 真实问题回归、备份、回滚、SLA 与生产安全验收仍未完成。

### 已解除：2026-08-10 Industry q008 曾实际运行旧 app revision

- `.60` 已正确加载 `docx-rag:2c4cf220c7cf`；该 tag 的 image ID 为
  `sha256:430e9df36c64a6596d43b1f463b5542b36623dc1adeb1d7d0d26357ed3f725a9`，OCI
  revision 为 `2c4cf220c7cf7dd2e8744253453e994ee7af3ee1`。但是运行中的
  `rag-industry-app` 仍配置为 `docx-rag:5ce587010422`，实际 image ID 为旧版
  `sha256:beff09b6290e10f45fcd8f734abdfd6d57868d2e43bfe2d7789e0474b5fd685f`；
  容器内 `build-info` 也明确返回 installed revision
  `5ce5870104220d992e6caf659004c7a51e52797d`、`matches=false`。因此 deploy 虽返回
  成功且 `/live`、`/ready`、活动索引均正常，新的 answer 代码实际没有进入运行容器。
- 部署输出中的
  `OCR_GPU_ALREADY_IN_USE` 来自 preflight 对已运行 dedicated OCR 的占用探测；后续
  install/deploy 与 OCR health 均成功，因此它不是当前 citation 失败的直接原因，但
  preflight 是否正确识别“当前 release 自己占用 GPU”仍需单独复核。
- 新一轮真实诊断中 001～007 均通过。005 已变为 `EXTRACTIVE_FALLBACK`，只引用
  GM-06 的两条真实原文，证明上一轮 source-label/正文过滤顺序修复已在某条运行路径
  生效。008 的新 Trace `86daa6a6ea204c8495c1ffd1647e8ab0` 仍为 answered，输出 4
  条 claim、`SOURCE_SEPARATED`，但全部 citation 来自 GM-04，预期 GM-03 缺失，故
  正式 `verify.sh` 继续报“工业正向问题缺少预期 citation”。禁止把该失败改写为验收
  通过，也不得放宽 expected source。
- 上一条 q008 Trace `38086d37279248f9ab9a2fcf550d3957` 已证明 GM-03 以 rerank
  rank 6、score 0.9727 进入最终 Evidence，GM-04 占 rank 1～5，模型最终只引用
  GM-04。source ID 由真实 DOCX 内容 SHA256 和稳定相对路径确定性映射。新增只读检查
  又确认 GM-04 原始 DOCX 正文中 `GM-03` 出现次数为 0，仅“质量管理制度”这个重合
  标题出现 3 次；因此不能解释为 GM-04 正文直接支持精确编号。
- q008 的四条已检索 Trace（包括 `86daa6a6ea204c8495c1ffd1647e8ab0`）全部记录 release
  revision `5ce5870104220d992e6caf659004c7a51e52797d`；每次 `cache.lookup` 均为 miss、
  `cache.wait` 均为 leader，随后实际调用 `llm.answer` 并由旧版 `answer.validate` 返回
  `VALIDATION_OK`。因此缓存恢复路径不是这次现场失败的原因，也没有任何一条 Trace
  验证过 `2c4cf220` 的精确编号规则。
- 根因现已收敛为部署未切换 app 容器，而不是 q008 新规则失效。下一步先只读核对
  `rag-industry.env` 的 `RAG_APP_IMAGE/RAG_RELEASE_REVISION/RAG_INDUSTRY_COMPOSE_FILE`
  和当前 Compose 展开后的 app image；确认有效配置确实指向 `2c4cf220` 后，只重建
  `rag-industry-app` 容器并再次核对 image ID、OCI/wheel revision，再复跑 q008 和
  20 问。OCR、Qdrant、活动索引与 corpus 均不得重建。deploy 在运行容器仍为旧
  revision 时也能返回成功，说明发布验收缺少“运行容器 revision 必须等于 env/release”
  的终态检查；本轮按用户要求仅记录该缺口，不修改部署脚本或回答代码。
- 后续只读检查确认 env 与 Compose 展开结果均已指向 `docx-rag:2c4cf220c7cf`；使用
  `--no-deps --force-recreate` 仅重建 `rag-industry-app` 后，运行容器 image ID 为
  `sha256:430e9df36c64a6596d43b1f463b5542b36623dc1adeb1d7d0d26357ed3f725a9`，
  容器内 installed revision 与 expected revision 均为
  `2c4cf220c7cf7dd2e8744253453e994ee7af3ee1` 且 `matches=true`。OCR、Qdrant 容器
  未重建，`/live`、`/ready` 与活动索引继续正常；未执行 `run-index.sh`。
  `verify.sh` 最终返回 `active_source_count=10`、`point_count=139`、
  `smoke_passed=20` 和 `RAG_INDUSTRY_VERIFY_OK`，证明 q008 与其余正向/负向隔离题在
  新 revision 下全部通过。本节保留为发布终态校验缺口记录，不再视为当前阻塞。

### 已解除：2026-08-10 training app-only 更新曾被 Industry 环境污染

- 在同一个交互 shell 中曾加载 Industry env，导出的 `RAG_PORT=8188` 保留在当前
  进程环境中。Compose 的 shell 环境优先级高于 training `--env-file` 中的
  `RAG_PORT=8088`，因此 training `rag-app` 被错误展开为宿主机 `8188`，与健康运行的
  `rag-industry-app` 发生端口冲突。该问题不是 app 镜像、索引、模型服务或 corpus
  故障；以后不得在部署两个实例的同一 shell 中 `source` 任一 env，验收脚本必须直接
  解析所需 token，且部署前后都要核对 Compose 展开端口。
- 首次 `update-app.sh` 因 `/live` 失败进入回滚，但现场仍留下使用新 image、错误映射
  `8188` 且持续 restarting 的 `rag-app`，training 的 `8088` 暂时不可用。说明回滚
  输出不能单独作为恢复成功证据，必须继续核对容器实际 image、端口、state/health 和
  `/live`、`/ready`；本轮边界不修改部署脚本，保留该终态校验缺口。
- 清除当前 shell 中全部导出的 `RAG_*` 后，training env 被重新确认仍指向既有 simple
  Compose、端口 `8088` 和旧 revision；重新执行同一个三文件 app-only 更新后返回
  `reindex_required=false`、`worker_restarted=false` 和
  `app_update_ok image=docx-rag:2c4cf220c7cf`。最终 `rag-app` 实际 image ID 为
  `sha256:32914eb418ecff0e806a29b766cd29cbdd81190dbeadda0aa4929c93fa45f6be`，容器内
  installed/expected revision 均为
  `2c4cf220c7cf7dd2e8744253453e994ee7af3ee1`，宿主端口恢复为 `8088`；Industry
  `8188` 同时保持健康。两个实例的 `/live`、`/ready` 均已通过。本节不再视为当前
  部署阻塞，剩余阻塞仅为下节所列真实六问、缓存、并发调度与 Trace ZIP 验收尚未执行。

### 当前决策卡点：training 新 revision 尚无真实业务回归证据

- 当前没有服务中断：training `rag-app` 已健康监听 `8088`，Industry
  `rag-industry-app` 已健康监听 `8188`，两者 build-info 均匹配完整 revision
  `2c4cf220c7cf7dd2e8744253453e994ee7af3ee1`。但是 `/live`、`/ready` 只证明依赖和
  活动索引可用，不能替代本任务要求的自由问法、回答校验、缓存、调度和 Trace 导出
  验收。因此当前卡点是“缺少新 revision 的真实请求证据”，不是“服务当前不可用”。
- 真实回归样本必须来自
  `C:\Users\jerry\Desktop\RAG\RAG_log\2、自由问题.zip`。已逐个用旧 Trace 中的
  `question_sha256` 反校验前五个文件名题面；第六个文件名对应的 JSON 与第五个文件
  payload 字节完全相同，内部问题摘要也仍是第五题。故旧包只有 5 个唯一 Trace，
  “快验流程中哪些研发环节可以由团队灵活处理，但项目交付流程不能省略？”仍没有任何
  独立基线或新 revision Trace，禁止把重复文件当作第六题通过。
- 当前尚未在 training `8088` 对 6 个真实题面生成 6 个新且互不相同的 trace ID；也
  尚未证明它们全部为 `ANSWERED`、`VALIDATION_OK`、正常路径
  `model_calls=1/retry_count=0`。尤其需要核对第一题与第三题为 `PROCEDURE`、第二题与
  第五题为 `DECISION`、第四题与第六题为 `COMPARE`，否则本轮新增自由问法意图仍不能
  视为服务器回归通过。
- 比较/决策语义仍缺现场证据：第四题必须是 `SOURCE_SEPARATED` 且 user message 为
  `下面按模式或来源分别列出。`，不得仅因多 source group 标记 `CONFLICT`；第五题和
  第六题的 `dropped_claim_codes` 不得包含 `CROSS_SOURCE_GROUP`。两题还需记录
  `first_validated_claim_ms` 并证明首条已验证 claim 早于最终完成；服务器 `<3 秒` 是
  当前性能目标，若未达到必须如实报告，不能以总耗时或 TTFT 代替。
- 支持质量诊断尚未在 training 新 Trace 中确认。每个最终回答的 SAFE Trace 应包含
  `selected_support_ranks`、`min_selected_support_score` 和
  `low_rank_support_count`，本轮只告警、不因 rank>4 或 score<0.2 自动删证据。特别是
  `OPC owner` 问题是否仍引用低 rank/低 score 证据，必须结合最终 answer 和 locator
  人工复核，不能只看分数直接判断证据无用。
- 缓存契约尚未现场复核：一个此前未命中的精确问题首次应正常经过
  retrieve/rerank/LLM，同一问题第二次才应为 exact cache hit；第二次 Trace 必须
  `model_calls=0`，且不得出现 retrieve、rerank、`llm.answer` span。不得清缓存、修改
  key/TTL，若固定问题因已有 v9 历史缓存首轮即命中，也必须记录实际情况而不能伪造
  `model_calls=1`。
- 四并发调度尚未现场复核：需要 4 个不同且未命中缓存的问题同时请求，至少分布到 2 个
  健康 Qwen endpoint，并逐条确认 `retry_count=0`。顺序请求持续选择 EWMA 最快的
  `.58:8001` 仍不属于故障；不能用顺序请求结果代替并发分布验收。
- 批量 Trace ZIP 尚未对这 6 个新 trace ID 验证。ZIP 必须只有 6 个
  `<trace_id>.json` 和唯一 `TRACE_EXPORT_MANIFEST.json`；manifest 中 trace ID 必须
  唯一，文件集合、`json_file`、JSON SHA256、created_at、status、question SHA256
  必须逐项一致，且文件名和 manifest 不得出现问题正文。重复 trace ID 的导出请求仍
  应返回 422。完成前不能宣称 Trace 导出真实服务器验收通过。
- 部署输出已经证明 `reindex_required=false` 且 worker 未重启，但仍需从上述新 Trace
  核对 pipeline/index fingerprint 继续为
  `sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a`。
  本轮按用户最新指令停止继续请求、验收和代码修改，只记录并报告这些卡点，等待用户
  选择后续方案。

### 当前保留的发布工具风险（运行已恢复，代码未修）

- Industry `deploy.sh` 曾在 env、Compose 和新 image tag 都正确时继续复用旧 app
  容器，并仅凭 health 返回成功；缺少“运行容器 image/revision 必须等于目标 release”
  的强终态门禁。以后再次部署 Industry 时，仍必须人工核对 configured image、实际
  image ID、OCI revision、容器内 wheel revision，必要时只对 app 使用
  `--no-deps --force-recreate`，不得由 deploy 成功字样直接判定切换完成。
- training `update-app.sh` 会受到调用 shell 中已导出 `RAG_*` 的 Compose 优先级影响；
  本次 `RAG_PORT=8188` 覆盖 training env 后造成端口冲突。脚本当前也未证明回滚后
  容器一定回到旧 image、旧端口和 healthy 状态。以后必须从全新 shell 执行，部署前
  明确清除外部 `RAG_*`，并把 Compose 展开端口、容器实际状态及两个实例的 `/live`、
  `/ready` 作为终态证据；在用户决定前不修改该脚本。
- Industry preflight 在本 release 自己的 dedicated OCR 已运行时仍可能打印
  `OCR_GPU_ALREADY_IN_USE`/`Address already in use`，而后续 install/deploy 可继续成功
  且 OCR 保持 healthy。该检测对“外部未知进程占用”和“当前受管 OCR 正常占用”的区分
  不充分，未来重部署仍可能产生误阻塞；它不是当前回答失败或服务健康问题，本轮也不
  修改 OCR、GPU 或 preflight 代码。
- 旧 release、transfer archive、data 目录与 app image 的清理清单尚未依据当前
  container、env、Compose 和 last-good 回滚引用完成。未形成逐项引用关系前不得删除；
  后续只列候选和保留理由，由用户自行删除。

## 语义路由真实校准与服务器回归

- 本轮按边界未访问 `.57/.58/.60`，没有调用真实 embedding 或 Qwen。因此
  `deployment/config/intent-router-calibration.json` 保持 `status=unverified`，
  `intent-router.json` 默认 `mode=shadow`；语义 profile 不会成为生产生效结果。
- 已提供 `evaluation/calibrate_intent_router.py` 与
  `evaluation/evaluate_intent_router.py`：前者只能以 tuning 集和当前配置的真实
  embedding endpoint 生成 verified 产物，后者以固定产物只报告 holdout 指标。实际
  校准必须记录 primary/secondary/slot macro F1、GENERAL fallback rate、coverage
  和全部身份 SHA；未经此步骤不得切换到 semantic/hybrid。
- app-only 更新已经完成，但仍须重新执行 6 个自由问题，确认 6 个不同 trace ID、
  ANSWERED/PARTIAL、VALIDATION_OK、q4 不为 CONFLICT、q5/q6 无
  `CROSS_SOURCE_GROUP`、正常 `model_calls=1/retry_count=0`、exact cache 命中不再
  retrieve/rerank/llm.answer，且 4 个并发不同问题至少使用两个健康 Qwen endpoint。
  顺序请求持续选择最快副本不是失败；不得重建索引，需核验 index fingerprint 不变。
- 本地 Trace export 的实际 Qdrant 集成专项受 `127.0.0.1:6333` 返回 502 阻塞；
  不修改 Qdrant、索引或导出逻辑，待可用的本地/授权环境复跑。
