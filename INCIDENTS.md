# Industry 事故与防复发记录

## 2026-08-11 serving update 使用仓库通用配置冒充真实 Industry source

- **影响：** `e5c98cead384` 包把 Git 中通用 config SHA 和
  `sha256:dd16e57...` index fingerprint 写为 source 合同。服务器实际挂载的是首部署
  builder 根据 GM-01～GM-10 生成并已被 release manifest 绑定的 Industry 专用 config，
  index fingerprint 为 `sha256:d2497bc...`。updater 因五文件 SHA 不同在
  `config_filesystem_precheck` 停止；没有 mutation，也没有破坏现有服务，但该包无法合法
  升级真实实例。
- **根因：** serving-update builder 根据旧 Git revision 的通用 config blob 推导 source，
  没有读取和自检服务器实际来源的首部署 release。测试夹具也复制 Git blob，重复了同一
  假设。Trace 预检另只允许 v1/v2；真实 2c4 源码从未设置 `PRAGMA user_version`，因此
  服务器 93 条、四表结构完整且 `quick_check=ok` 的 v0 被错误拒绝。
- **修复：** builder 必须先通过首部署 package selfcheck，再精确绑定 release ID、revision、
  manifest SHA、五份 config SHA、package contract、index 和 serving fingerprint。target
  从该 Industry config 复制，只更新实际 prompt revision；目标 config 和对应临时资产清单
  同时进入 app image 与 runtime archive。v0 预检比较完整的 2c4 表/列/索引/外键结构，
  在线备份记录 Trace 条数，目标 v2 迁移后再次验证 schema、quick-check 和条数不减少。
- **防复发证据：** 独立红测证明旧 helper 会接受任意 v0 且没有备份条数。builder 测试固定
  真实 source manifest/config/index/serving 身份并断言 target 仅 `pipeline.json` 改变；
  updater 脚本沙箱使用真实 Industry config 和 93 条精确 v0，正常路径迁移为 v2 后仍为
  93 条，完整 74 个更新/回滚/中断场景通过。`e5c98cead384` 与 `a50d5d5f8f71` 永久失效。

## 2026-08-11 人工撤回把 target 故障当成拒绝回滚条件

- **影响：** `a50d5d5f8f71` 的 post-verified 人工撤回在任何 mutation 前要求 target
  health/ready、容器内 build-info 和 runtime-state 全部成功。target unhealthy、
  ready=503、stopped、missing 或 runtime-state 暂时不可执行时均无法恢复 source；更严重
  的是失败路径调用 `rollback_abort()`，把仍在线且未修改的 `verified` transaction 写成
  `rollback_failed`，瞬时故障也会永久阻止安全重试。
- **根因：** target 的不可变控制面身份与可用性证据没有分层；同一个失败处理器同时处理
  mutation 前的只读检查和 `rolling_back` 后的恢复故障，导致审计状态早于实际事务边界。
- **修复：** manual rollback 先严格核对 candidate env、manifest/update ID、目标 image
  ID/OCI/platform/ENTRYPOINT/已验证 build-info、verified-state、target-contract、pointer、
  source snapshot、index 和依赖身份。存在的 target 容器仍校验 configured/running image、
  project/service 和 env revision；身份错误 fail closed。health/readiness/runtime-state 只作
  可用性分类和增强证据，target unhealthy/stopped/missing 时直接进入 source 恢复，不重建
  target。成功 precheck 先 fsync 0600 的 `manual-rollback-precheck.json`，随后才写
  `rolling_back`；此前失败保持 `verified`，只有 mutation 阶段失败写 `rollback_failed`。
- **防复发证据：** 脚本级测试从完整 `verified` 更新注入 healthy、unhealthy、ready=503、
  stopped、missing、runtime-state 不可用、错误 image/revision/project/service、env/pointer/
  index/dependency 漂移、瞬时失败重试与 env 恢复后失败。首次红灯为
  `20 failed, 3 passed, 50 deselected in 78.77s`，没有用 skip/xfail 或放宽身份门禁消除。

## 2026-08-11 post-verified 人工撤回遗留 target pointer 且绕过全局锁

- **影响：** 已验证 target 被人工撤回时，旧入口只恢复 source env/App，不把
  `last-good-pointer.json` 指回 source snapshot；运行身份与回滚权威点相互矛盾。同一入口
  也没有取得 updater 的共享锁，因此可与更新并发修改同一 env 和 App。
- **根因：** 自动失败回滚与验收后的运维撤回共用一段不理解 verified target pointer 的
  脚本，且全局锁只写在 updater 内部，没有形成可复用的实例级合同。
- **修复：** 公共 rollback 先取得同一 `serving-update.lock`，再调用只允许
  `--manual-verified` 或 updater 内部 `--automatic-failure` 的 core。人工撤回严格验证
  target runtime/index/dependency/pointer，原子恢复 source env、只重建并复验 source App，
  最后把 pointer 原子发布为原事务密封的 source snapshot；target snapshot 保留，事务只有
  在 `rolled_back` 成功落盘后才报告成功。
- **防复发证据：** 脚本级测试覆盖 target→source pointer、target snapshot 保留、重复撤回
  拒绝、同包新 attempt、pointer/source snapshot/index/dependency 漂移、状态落盘失败以及
  update/rollback 双向锁竞争。

## 2026-08-11 App 激活缺少持久化意图，硬中断后无法判定 env/App 组合

- **影响：** private env 已原子切到 target、`activated` 尚未落盘，或状态已落盘但 App
  recreate 尚未完成时发生 SIGKILL/掉电，旧 updater 会留下 `prechecking/activated` 与实际
  env/App 不一致；重入只报 `CURRENT_ATTEMPT_INVALID`，无法安全继续或回滚。
- **根因：** 原子 env rename、transaction state 和 Docker recreate 是三个独立持久化域，
  激活前没有 fsync 的 write-ahead intent，也没有按 env SHA、App identity 和健康态定义
  确定性恢复矩阵。
- **修复：** 激活前写入并 fsync 不含 secret 的 `activation-intent.json`，状态机增加
  `activating`，env rename 后验证 SHA 再写 `activated`。重入使用 source/target env SHA、
  App image/ref/revision、Compose/config 身份和 Docker health 分类；健康 target 不重复
  recreate，不健康 target 只补做一次，混合状态自动回滚 source，未知状态写
  `rollback_failed` 并 fail closed。
- **防复发证据：** 故障注入覆盖 intent 前中断、env rename 后中断、activated 后中断、
  Compose 已切换后中断、恢复过程再次中断、健康/不健康 target 重入、混合身份和未知 env
  SHA；完整 updater 脚本专项为 `58 passed in 195.07s`。

## 2026-08-11 真实旧版只有 last-good.env，升级器误要求 JSON pair

- **影响：** 真实 `2c4cf220...` 部署只写 `last-good.env`；`195f9aca2c63` 升级器把这种
  合法历史状态当成损坏 pair，更新在激活前报 `LEGACY_LAST_GOOD_PAIR_INVALID`，无法从
  服务器真实基线升级。
- **根因：** 新实现根据当前理想状态推断历史合同，没有先检查旧 Git 部署脚本，测试也只
  造了 pointer/pair，没有注入旧版实际产生的 env-only 目录。
- **修复：** 更新前先严格读取当前 source env、旧镜像、Compose、外部 config、index 和
  worker 身份，生成 canonical source state 与密封 snapshot。env-only 仅在文件为 0600
  普通文件且字节 SHA 与当前 source env 完全一致时接受；保留原始 env 字节，不补写伪造
  的旧 state/pointer。legacy pair、可信旧 pointer、当前 source pointer 与 pointer 缺失
  分别处理，state-only、symlink、重复键、权限或 SHA 漂移全部拒绝。
- **防复发证据：** 测试直接读取 `2c4cf220...` 的 `deploy.sh` 证明只写 env；env-only 正向
  与内容不符、0644、symlink、pair/state-only、未知 revision 等负向均受测。

## 2026-08-11 validated 恢复错误地只接受已晋升 target pointer

- **影响：** 完整 verify 已落盘并写到 `validated`，但进程在 last-good promote 前中断时，
  pointer 合法地仍指 source；`195f9aca2c63` 重入却报 `LAST_GOOD_POINTER_MISMATCH`，形成
  无法自动完成、也不应盲目覆盖的崩溃窗口。
- **根因：** reconcile 只建模“target 已完成”一种重入状态，没有把更新前 source pointer
  或 pointer absent 写入事务证据，也没有在恢复晋升前重新确认当前运行时仍是已验目标。
- **修复：** source checkpoint 固定 source pointer/absent、source snapshot 与完整文件
  identity；finalize 只接受精确记录的 source、精确 target 或记录为 absent 且仍 absent。
  `verifying`、`validated`、`verified` 恢复前均重新从目标 App 导出 runtime-state，并与
  pre-index、target contract、verified-state、UPDATE_MANIFEST、活动 index 及 UI/Trace
  合同严格交叉核对；恢复不再次 force-recreate App。
- **防复发证据：** source→target、target 幂等、absent→target、第三方 pointer、损坏 source
  snapshot、verifying 有/无 verified-state、validated 与 verified 重入均有故障注入测试。

## 2026-08-11 Trace 在线备份把合法 WAL 写入误判为源文件篡改

- **影响：** SAFE Trace 在 SQLite WAL 模式下合法并发提交会改变主库 size/mtime 或 WAL
  bytes；旧备份用完整 `stat` 前后相等作为门禁，因而报 `TRACE_BACKUP_SOURCE_MUTATED`
  并中止本来安全的 app-only 更新。
- **根因：** 文件身份与数据库活动观测混成一个不可变对象，忽略 SQLite online backup
  允许源数据库在备份期间继续提交的合同。
- **修复：** 稳定身份只包含 device、inode、UID、GID、file type 和 mode，前后必须一致；
  主库 bytes/mtime 与可选 WAL bytes 作为易变观测写入 schema v2 报告。仍使用
  `sqlite3.Connection.backup()`，完成后重新只读打开目标，验证 SHA、页数、
  `user_version` 与 integrity；文件替换、symlink、类型、owner 或 mode 漂移 fail closed。
- **防复发证据：** 正常在线备份、真实 WAL 并发提交、源文件替换、symlink、权限与 owner
  漂移均有专项测试；并发用例不暂停 Trace writer，也不降级为离线复制。

## 2026-08-11 不同 update ID 缺少共享锁而可并发修改同一服务

- **影响：** attempt 锁只位于各自 update ID 目录；两个不同候选包可以同时通过预检并
  修改同一 env、App、Trace 备份和 last-good，事务证据彼此独立却争用同一运行时。
- **根因：** 将 update ID 误当成资源隔离边界，没有在所有更新共享的 backup root 建立
  全局互斥；审计目录又在取得互斥前创建，失败竞争也会留下误导性 attempt。
- **修复：** updater 在创建 update audit root/attempt 前，对 backup root 下固定
  `serving-update.lock` 取得非阻塞 `flock` 并持有到进程退出。backup root 和锁文件拒绝
  symlink，锁必须为 0600 普通文件，打开 descriptor 与路径 inode 再核对；缺少 `flock`
  或竞争失败立即退出，不创建新的更新事务。
- **防复发证据：** 两个不同且各自有效的目标包真实并发时只有一个进入事务；同包并发、
  锁/backup symlink、锁权限错误、缺少 flock 和释放后重试也全部覆盖。

## 2026-08-10 Compose revision 切换被同 env 夹具假绿掩盖

- **影响：** `8755bf379c8f` 包将真实旧 revision 切换到目标 revision 时，旧、新 App
  与 worker 的 `RAG_RELEASE_REVISION` 必然不同，但 helper 把它当成未授权环境漂移并在
  激活前报 `APP_ENVIRONMENT_CHANGED`；服务不会损坏，但候选永远无法上线。
- **根因：** 旧测试用同一份 `.env.example` 渲染 old/new Compose，fake canonical
  environment 又缺少 revision 和五个 config path，因而没有复现真实 source/candidate
  身份差异。
- **修复：** Compose helper 显式接收 source/target revision、config 与 image，App 和
  worker 分别核对 40 位 SHA，只允许声明的 revision 迁移及既有七项 UI/Trace 目标变化；
  其他环境、Qdrant/OCR、network、port 和 volume 仍 fail closed。
- **防复发证据：** 测试从 Git 读取真实 `2c4cf220...` Compose，用独立 old/candidate
  env 和 Docker Compose v5.1.2 渲染；合法迁移通过，非法 revision 和额外差异失败。

## 2026-08-10 SAFE Trace 异步落库造成验收瞬时误回滚

- **影响：** 普通 UI 请求返回唯一末尾 `final` 时，SAFE Trace 可能仍在 writer 队列；
  旧验收立即查询 list/detail，会把短暂的空 list、RUNNING、404 或问题字段未写完误判为
  App 故障并触发回滚。
- **根因：** 验收错误地把“写命令已入队”等同于“SQLite 已可通过 Admin API 读取”，且
  测试只覆盖同步可见结果。
- **修复：** 不改变生产 TraceRecorder 异步语义；验收改用单调时钟和固定上限轮询，仅对
  空 list、RUNNING、detail 404、问题字段尚不可见重试。错误/重复 trace ID、鉴权/422/
  5xx、终态问题或 SHA 不一致立即失败，报告只含次数和耗时。
- **防复发证据：** 三次查询后可见、永久不可见超时、401/422/500、错误 ID、问题和 SHA
  不匹配均有专项测试。

## 2026-08-10 precheck 卡死与 rollback 状态伪成功

- **影响：** `8755bf379c8f` 在 filesystem、index、Trace backup、Compose、image load 或
  asset-selfcheck 任一步激活前失败时，attempt 永久停在 `prepared` 且同包不能重试；
  rollback 成功后的状态写入又使用 `|| true`，可能打印成功但审计仍是非终态。
- **根因：** 错误处理安装得过晚，状态机没有区分 precheck 失败与已激活失败，也没有把
  审计状态持久化本身当作事务门禁。
- **修复：** 状态扩展为 `prepared/prechecking/precheck_failed/activated/verifying/
  validated/verified/rolled_back/rollback_failed`；创建 attempt 后立即安装退出处理。
  激活前失败写稳定 stage/code 的 `precheck_failed`、不回滚并允许新 attempt；状态写入
  失败返回独立错误，未确认 `rolled_back` 时绝不宣称成功。
- **防复发证据：** 六类 pre-activation 故障、同包第二 attempt、rollback state write
  失败、非终态/未知状态拒绝和两份证据保留均由脚本级故障注入覆盖。

## 2026-08-10 verify、last-good 与 transaction 之间存在崩溃窗口

- **影响：** 旧 verify 会先晋升 last-good，再由父 updater 写 `verified`。两步之间崩溃
  会留下“pointer 已是目标、transaction 仍 verifying”的不可恢复组合；同时
  `resolve 2>/dev/null || true` 会把损坏 pointer 当作不存在并可能被新晋升覆盖。
- **根因：** 子 verify 同时承担验收和控制面提交，父事务没有可恢复的 validated
  checkpoint；pointer 缺失与 pointer 损坏也没有分支分类。
- **修复：** verify 只生成并 fsync `verified-state.json`；父 updater 先写 `validated`，
  再由独立 finalize helper promote/reconcile、严格 resolve 并逐字节核对 env/state/
  revision，最后写 `verified`。validated 重入只做目标轻量身份检查；pointer 已存在但
  JSON、snapshot、manifest 或 SHA 损坏一律 fail closed。
- **防复发证据：** 在真实 promote 后、写 verified 前杀进程，再次执行可完成 reconcile；
  pointer 目标不一致和四类损坏均拒绝覆盖。

## 2026-08-10 source config 未绑定且 0600/0644 升级合同冲突

- **影响：** 旧 updater 只检查五个 config SHA 的格式，没有把服务器实际内容与兼容源
  revision 绑定；手工漂移仍可能被接受。另一方面首次部署 config 为 0600，而成功的
  serving runtime config 为 0644，单一 `mode==0600` 会阻塞下一次合法更新。
- **根因：** manifest 缺少由权威源提交导出的 source exact SHA 和显式 mode profile，
  filesystem helper 把首次部署权限形态误当成永久唯一形态。
- **修复：** builder 通过 `git show <source revision>:<path>` 读取真实五文件并计算 SHA；
  manifest 同时绑定 source revision、serving/index fingerprint 和 profile。helper 支持
  `first-deploy-private-v1` 的 0600 与 `serving-runtime-public-config-v1` 的 0644，二者
  都要求普通文件、非 symlink、非 group/other writable、exact SHA 和只读挂载。
- **防复发证据：** 0600、0644 正向及 0664/0666、symlink、SHA 漂移负向测试全部覆盖。

## 2026-08-10 本地 Qdrant 长复用与 SQLite WAL 测试夹具波动

- **影响：** 同一个无卷临时 Qdrant 长时间复用的第二轮关联测试出现一次 HTTP connect
  timeout；损坏 SQLite 用例只替换主文件但保留 WAL/SHM 时，SQLite 可从日志恢复，导致
  “预期损坏”偶发不成立。两者都不是 production StateStore 或索引语义变更。
- **处置：** 记录两次非绿结果，不删除证据；损坏夹具在替换主库后同时删除该测试库的
  WAL/SHM，确保输入确实损坏。Qdrant 使用新建、无卷、空 collection 的固定 v1.18.3
  容器重跑，专项结束再次确认 collection 为空。
- **最终证据：** 两个复现用例先单独通过，扩大 Qdrant 关联集 `72 passed, 56 warnings
  in 386.94s`，随后最终全量 `1203 passed, 61 warnings in 713.97s`。

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
