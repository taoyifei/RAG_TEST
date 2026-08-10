# Industry 事故与防复发记录

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
