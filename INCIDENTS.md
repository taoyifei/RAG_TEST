# Industry 事故与防复发记录

## 2026-08-10 真实 App 镜像自检重复 ENTRYPOINT

- **影响：** `d5c03cf9b97e` updater 在切换 env/App 前执行
  `docker run IMAGE rag-app asset-selfcheck`；镜像已固定
  `ENTRYPOINT ["rag-app"]`，真实命令会变成 `rag-app rag-app asset-selfcheck` 并
  fail closed，候选包无法在服务器进入更新阶段。
- **根因：** fake Docker 只检查参数中出现 `asset-selfcheck`，没有按 Docker 规则组合
  ENTRYPOINT 与 CMD，因此字符串层绿测掩盖了真实 CLI 错误。
- **修复：** updater 改为 `docker run --rm --network none IMAGE asset-selfcheck`，并在
  同一阶段严格核对 image ID、`linux/amd64`、OCI revision、
  `Entrypoint=["rag-app"]` 和 `build-info --expected-revision`。
- **防复发证据：** 真实 `docx-rag` 镜像执行正确命令成功，重复 `rag-app` 命令非零；
  静态测试禁止 deployment 脚本再次出现该错误组合。

## 2026-08-10 updater 宿主直读 UID 10001 私有文件

- **影响：** 首次安装后的 config 与 Trace SQLite 为 10001:10001、0600，普通部署用户
  无权由宿主 Python 计算 config SHA、在线备份 Trace 或读取 schema，导致真实 canary
  在切换前 `PermissionError`。
- **根因：** 既有 fixture 由 pytest 用户创建，未复现容器 owner/mode；updater 又把
  container-owned 业务文件误当成宿主可读控制文件。
- **修复：** `pre-update-filesystem-state`、`backup-trace-database` 与 `trace-schema`
  全部在一次性 App service container 内运行。config/Trace 源只读；Trace 仍使用
  `sqlite3.Connection.backup()`，输出经受控 UID/GID chown、chmod 0600 和 fsync 后由
  宿主操作员持有，不改变长期 App user/cap_drop 或源 owner/mode。
- **防复发证据：** 真实 Docker fixture 把目录和文件设为 10001:10001/0600，宿主
  UID 1000 对目录和 DB 均得到 `PermissionError`；默认 UID helper 可读，Compose 5.1.2
  的一次性 root + `DAC_OVERRIDE/CHOWN` 可完成在线备份，源 stat 不变且备份宿主可读。

## 2026-08-10 Compose 端口比较依赖极简测试 JSON

- **影响：** 旧、新 Compose 即使端口语义同为 8188→8088，也会因真实 canonical JSON
  带 `protocol/mode/host_ip` 被极简 dict 等式拒绝；反之 volumes 权限变化又可能因只看
  source/target 而漏检。
- **根因：** fake Docker 固定返回测试专用两字段对象，没有使用真实
  `docker compose config --format json`，也没有明确规范化端口和 bind mount 合同。
- **修复：** 新增独立标准库 `compose_check.py`：规范化 target/published、tcp、ingress
  和 wildcard host，校验唯一端口；volumes 比较 type/source/target/read_only 与其余
  canonical 字段；依赖服务、networks 和未授权 App/worker 结构变化继续 fail closed。
- **防复发证据：** 使用 Git 中旧 `2c4cf220` Compose、当前 Compose 和真实 Compose
  5.1.2 canonical JSON 运行测试；额外默认字段通过，第二端口、UDP、不同 host IP、
  未知字段和读写权限变化均被拒绝。

## 2026-08-10 日志泄漏验收在请求前截取快照

- **影响：** `d5c03cf9b97e` 先保存 `docker logs`，再发送 Bearer/UI/Trace 请求；即使
  本轮问题或 token 被新请求写入日志，旧快照也不会包含它们，验收形成假绿灯。
- **根因：** UI/Trace 行为检查与日志反查耦合在一个命令，但日志生命周期位于行为检查
  之前；NDJSON 解析又只收集 trace ID，没有要求唯一且终止流的 `final` 事件。
- **修复：** 拆为 `verify-ui-trace` 与 `verify-log`。先记录向前留两秒的 UTC 边界并完成
  UI/Trace 请求，再以 `docker logs --since` 捕获新增日志，最后反查固定问题、Query
  Token 和 Admin Token；固定问题只存在 Python 模块内部，不进入 shell 参数。
- **防复发证据：** 请求后日志含问题或任一 token 均失败，安全旧内容通过；NDJSON
  缺失、重复或非末尾 `final` 及 trace ID 不一致均失败，脚本顺序测试固定为
  request → log capture → redaction verify。

## 2026-08-10 回滚成功后同包更新无法重试

- **影响：** 旧 updater 以 update ID 固定单一 transaction 目录；第一次候选验收失败并
  成功回滚后，审计目录按要求保留，但第二次执行同一个包立即报
  `UPDATE_TRANSACTION_ALREADY_EXISTS`，无法进行真实修复后的重试。
- **根因：** transaction 只有“目录存在/不存在”两态，没有持久化 attempt 编号和终态，
  因而无法区分已验证、已回滚、回滚失败、未知中断和可安全创建的新尝试。
- **修复：** update ID 作为审计根目录，每次执行写独立 `attempt-000N`；0600 状态文件以
  canonical JSON 和原子 rename 记录
  `prepared/activated/verifying/verified/rolled_back/rollback_failed`。只有上一 attempt
  为 rolled_back 才允许新建下一项；rollback_failed、未知或非终态均要求人工复核，
  verified 目标只运行幂等 verify。
- **防复发证据：** fixture 中第一次验收失败并完成真实回滚，第二次同包成功；两份
  transaction 证据和终态均保留。成功后再次执行走幂等路径；rollback_failed、未知状态
  和 attempt 序列不连续均被拒绝，last-good 只在最终 verify 后晋升。

## 2026-08-10 四文件 App 包无法从真实旧版安全升级

- **影响：** `artifacts/industry-app-update/809fb71f5e50` 只有 App image、sidecar、
  updater 和命令文件；在旧 revision `2c4cf220...` 上会于切换前要求不存在的
  `runtime-state`，即使绕过也缺少新 config、Compose、helper 和 verify，可能出现镜像
  已更新但 UI/Trace 合同未生效或新 App 因 prompt revision 不匹配拒绝启动。
- **根因：** 旧包把更新等同于替换镜像，未把外部只读 serving config 和 Compose 运行
  合同纳入同一版本身份，并错误假定旧 App 已提供目标版本 CLI；终验还依赖旧 release
  的脚本。
- **修复：** 废止四文件合同，改为 8 文件 Industry serving update。确定性 runtime
  archive 交付新 Compose、5 份 config、更新前 identity/Trace backup helper、包内
  verify/rollback 和 validation；旧身份由注入旧镜像的一次性标准库 helper 只读取得，
  新 App 的 runtime-state v2 再核对 target serving/UI/Trace/index 身份。
- **防复发证据：** `tests/test_industry_upgrade_from_2c4cf220.py` 固定旧版无新命令、旧
  Compose/config 语义；builder、safe extraction、updater 正常/失败恢复和 tamper 测试
  覆盖顶层/runtime exact set、只重建 App、包内 verify、index 不变和旧身份恢复。旧
  `809fb71` 制品被文档明确标为不可上传。

## 2026-08-10 Compose 环境污染导致 8088/8188 串线

- **影响：** training `rag-app` 曾被展开为 host port 8188，与已运行的
  `rag-industry-app` 冲突；失败回滚后 training app 一度重启循环且 8088 不可用。
- **发现：** 用户提供的容器 inspect 显示 training service 的 published port 实际为
  8188，而 Industry 正常占用 8188。
- **根因：** Compose 虽传入 `--env-file`，仍会让调用 shell 中遗留的 `RAG_*` 覆盖文件
  值；旧脚本没有先校验 canonical config，也没有在回滚后核对实际容器身份和端口。
- **修复：** simple 与 Industry 脚本通过 `env -i` 仅白名单传递 Docker 必需环境，显式
  使用 `-p rag-simple|rag-industry`、`--env-file` 和 `-f`；执行 `up` 前严格解析 Compose
  JSON，app 使用 `--force-recreate`，成功和回滚都核对 image、image ID、revision、
  project、service、容器 env、端口、build-info、live 和 ready。
- **防复发证据：** 污染 `RAG_PORT`、`RAG_APP_IMAGE`、`RAG_QDRANT_ALIAS` 的模拟测试已
  纳入 `tests/test_simple_deployment.py`、`tests/test_industry_deployment.py` 和 updater
  合同；本轮专项 `135 passed`，全量 pytest `1158 passed`。

## 2026-08-10 updater 用文本工具解析 JSON

- **影响：** Industry updater 的正常路径和失败回滚测试在更新前即报 index fingerprint
  不一致，无法证明 force-recreate 和真实回滚门禁。
- **发现：** 合法 JSON 在冒号后带空格时，旧 `sed` 模式解析为空；两条红测稳定复现。
- **根因：** 把 JSON 当作固定排版文本解析，未验证字段类型、SHA256 格式或 exact set。
- **修复：** updater 全部使用 Python 标准库 `json` 严格解析 asset report、runtime
  identity 和 update manifest；验证对象/布尔/字符串类型、40 位 revision、64 位 SHA、
  `sha256:<64hex>` fingerprint、目标 service 和 `reindex_required=false`。
- **防复发证据：** 紧凑/带空格 JSON、字段类型错误、摘要格式错误、正常更新和失败恢复
  均有专项测试，最终 updater/deployment 套件全绿。

## 2026-08-10 配置已指向新镜像但实际容器仍运行旧镜像

- **影响：** 历史 Industry deploy 曾返回成功，`/live`、`/ready` 也正常，但实际 app
  仍是旧 image/revision，新回答修复没有进入运行路径。
- **发现：** env、Compose configured image、容器 `.Config.Image`、`.Image` 和容器内
  `build-info` 相互矛盾。
- **根因：** 旧 deploy 没有强制重建 app，且只用健康探针代替 desired/observed identity
  验证。
- **修复：** app 单独 force-recreate；identity 门禁同时比较配置 image、实际 image
  ref/ID、OCI revision、容器 env revision、wheel build-info、Compose project/service、
  port、mount、live/ready。回滚恢复旧 env 后再次执行同一组门禁。
- **防复发证据：** fake Docker 覆盖“新镜像已 load 但旧容器仍运行”和“新身份失败后旧
  身份必须真实恢复”，不是只检查脚本输出文本。

## 2026-08-10 last-good 过早晋升与 OCR 自占用误报

- **影响：** 历史 deploy 在 full index/smoke 前写 last-good，失败 release 可能覆盖可靠
  回滚点；健康的受管 Industry OCR 占用目标 GPU 时又可能被 preflight 当作外部冲突。
- **根因：** 部署阶段与验收阶段没有持久化状态边界；GPU 检查只看 PID，没有核对容器
  所有权、镜像、revision、GPU 和健康状态。
- **修复：** 原子状态机固定为
  `candidate -> deployed -> indexed -> verified -> last_good`，只有 verify 的完整现场
  门禁成功才晋升；OCR 检查区分空闲 GPU、受管健康 OCR、未知 PID、training OCR、旧
  OCR 和 external 模式，任何未知归属 fail closed。
- **防复发证据：** `tests/test_industry_deployment_state.py` 覆盖 index/smoke 失败、晋升
  中断、rollback 恢复和 candidate 不得冒充 last-good；部署矩阵覆盖 OCR ownership。

## 2026-08-10 pipeline 资产漂移

- **影响：** 源码树 asset-selfcheck 曾明确报
  `deployment/config/pipeline.json` SHA256 不一致，不能据此构建可信 app image。
- **根因：** prompt/serving 语义变化后 pipeline 和前端资产已变化，清单尚未在最终语义
  核对后更新。
- **修复：** 先核对实际 prompt revision、corpus policy semantic SHA、router 状态、
  calibration 状态、index/serving fingerprint；确认 index fingerprint 不变、serving
  fingerprint 应变化后，只更新实际变化的 `frontend/debug.js` 清单摘要。
- **防复发证据：** 源码树 `asset-selfcheck` 验证 13 个文件，SHA 清单全量 check 通过；
  最终包仍必须在构建后容器内和 fresh extraction 后各复验一次。

## 2026-08-10 全量 pytest 的环境假红与 failover 合同回归

- **影响：** 旧记录为 `1010 passed, 85 failed`，无法满足 production test gate；一次
  为隔离 Qdrant 而使用的精简只读 Python 容器又产生 `172 failed, 986 passed`，其首错
  是容器缺少测试明确调用的 `/usr/bin/fakeroot`，不能把该结果归因于代码。
- **发现：** Qdrant 失败属于本地端点环境；10 个 model/OCR 失败则是真实合同回归：
  embedding、reranker 和 OCR 的可重试 endpoint 数据错误没有继续 failover，而生成式
  请求又必须保持“收到无效响应后不跨副本重复生成”的旧安全边界。
- **修复：** 使用固定 `qdrant/qdrant:v1.18.3`、无业务数据、无 volume 的一次性本地
  实例运行集成测试并在结束后清理；只为 embedding、reranker、OCR 显式启用 invalid
  response failover，默认仍立即拒绝，避免 LLM 重复生成。新增 malformed JSON、错误
  content-type、4xx、408/425/429、5xx、timeout、connection error 和 schema mismatch
  边界测试。
- **防复发证据：** Qdrant 专项 `62 passed, 56 warnings in 382.60s`，model/OCR 合同
  `64 passed, 1 warning in 1.37s`；在 WSL 完整依赖环境、同一临时 Qdrant 上执行全量
  pytest，最终 `1158 passed, 61 warnings in 647.91s`，没有 skip/xfail。测试容器、
  网络和临时目录均已清理。该绿测仍不替代服务器 acceptance 或生产校准。
