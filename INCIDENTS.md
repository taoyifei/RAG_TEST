# Industry 事故与防复发记录

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
  合同；最终专项套件 `256 passed`。

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
  fingerprint 应变化后，只更新 5 个真实漂移文件的清单摘要。
- **防复发证据：** 源码树 `asset-selfcheck` 验证 13 个文件，SHA 清单全量 check 通过；
  最终包仍必须在构建后容器内和 fresh extraction 后各复验一次。

## 2026-08-10 全量 pytest 的环境与既有合同红灯

- **影响：** 额外全量执行不能宣称绿色，真实结果为
  `1010 passed, 85 failed, 61 warnings in 696.58s`。
- **发现：** 大量 Qdrant 集成测试访问 `127.0.0.1:6333` 收到 502；另有 runtime 测试
  fixture 未提供新增 intent-router 路径，以及本任务前已存在的 model client/OCR schema
  错误分类测试与实现不一致。
- **处理：** 本轮相关 runtime fixture 已补齐并复跑 `32 passed`；Qdrant 按任务边界不
  启动、不修改、不重建；未触碰的 model client/OCR 合同红灯保留在 `BLOCKED.md`，不以
  skip/xfail 或越界改代码掩盖。
- **结论：** 本任务规定的 256 项专项、静态、Compose 和源码 asset 门禁已通过，但全量
  pytest 仍须明确报告为未通过，不能作为 production readiness 证据。
