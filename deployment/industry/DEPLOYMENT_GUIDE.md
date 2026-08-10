# Industry RAG 部署与 Serving 更新指南

这是一套与 `rag-simple` 培训实例并行运行的第二套 simple 部署。它复用同一个
`rag-app` 代码和模型服务合同，但使用独立的 Compose project、容器、端口、Qdrant、
alias、state、corpus、config、日志和 Token。不要把工业文档复制到培训 corpus，也不要
把两个 corpus 建入同一个 collection。

当前首版明确运行在 `demo` 模式，retrieval 为 `provisional`，intent router 为
`shadow`，calibration 为 `unverified`。本地构建和服务器 smoke 不能替代生产校准。

本次交付类型为 `industry-first-deploy-reuse-images`。它只交付新 app 与工业
corpus，复用目标服务器已经存在且身份完全匹配的 OCR/Qdrant 镜像。复用镜像只省略
重复传输，不会复用培训容器、volume、网络、alias 或数据目录。

## 包内权威文件

- `app-image.tar.gz`：app 与单次索引 worker 共用镜像；
- `corpus.tar.gz`：仅含 GM-01～GM-10 的审计 DOCX，10 active、0 reference；
- 不含 `ocr-image.tar.gz` 和 `qdrant-image.tar.gz`；对应 tag、image ID、平台及
  OCR revision 固定在 `RELEASE_MANIFEST.json`，服务器预检不一致即停止；
- `RELEASE_MANIFEST.json` 与 `SHA256SUMS`：release 身份和 exact payload；
- `package_selfcheck.py`：无网络 package 校验与安全解包；
- `preflight.sh`、`install.sh`、`deploy.sh`、`run-index.sh`、`verify.sh`、
  `rollback.sh`：分阶段、fail-closed 的 Industry 操作入口。

## 后续 Serving App Update

已完成首次索引的 Industry 只使用新的 8 文件 serving update 包，不再使用历史四文件
`industry-app-update`。历史 `artifacts/industry-app-update/809fb71f5e50` 缺少新
serving config、Compose 和包内 helper，且错误要求旧 App 支持 `runtime-state`，不得
上传或部署。复核后的
`artifacts/industry-serving-update/d5c03cf9b97e` 又存在真实镜像 ENTRYPOINT、
UID 10001 私有文件、canonical Compose 和日志时序四个 P0，同样永久失效且不得上传。

新包是 simple serving app update，不是 full release。顶层 exact set 为：

- `UPDATE_MANIFEST.json`；
- `app-image.tar.gz` 与 `.sha256`；
- `serving-runtime.tar.gz` 与 `.sha256`；
- `update-app.sh`；
- `package_selfcheck.py`；
- `SERVER_UPDATE_COMMANDS.txt`。

`serving-runtime.tar.gz` 将版本化 Compose、包内 verify/rollback/helper、5 份 config
和脱敏 validation 作为同一身份交付。runtime exact set 当前为 17 个文件；新增的
`compose_check.py` 只用 Python 标准库规范化真实 Compose canonical JSON，不访问业务
文件。它不包含 corpus、DOC/DOCX、OCR/Qdrant image、
secret、真实服务器地址或问题正文。更新前由包内标准库 helper 在旧 App image 的一次性
容器中只读取得 index identity，并用 SQLite online backup 备份 Trace；不要求旧 App
提供新 CLI。

服务器执行时从全新 shell 开始，不 source private env，按包内
`SERVER_UPDATE_COMMANDS.txt` 的占位变量填写绝对路径。安全顺序是 sidecar 与 package
selfcheck、更新前身份和 Trace 备份、原子安装版本化 runtime/candidate env、加载 App
image、仅 force-recreate `rag-industry-app`、runtime-state v2、包内
`verify-app-update.sh`。失败调用包内 `rollback-app-update.sh` 并复验旧身份。
每次执行保留独立 `attempt-000N/transaction-state.json`，状态只允许
`prepared/activated/verifying/verified/rolled_back/rollback_failed`。回滚成功后的同包
重试创建新 attempt，不删除旧证据；`rollback_failed`、未知状态或中断的非终态均
fail closed，需要人工复核。last-good 只在完整 verify 成功后晋升。

该更新明确禁止运行 `run-index.sh`，不启动或重启 worker/OCR/Qdrant，不修改 alias、
manifest、collection、point、answer cache 或 GM corpus。index fingerprint 必须保持
不变，`UPDATE_MANIFEST.json` 的 `reindex_required` 必须为 `false`。当前配置仍是
demo/canary；本地 package selfcheck 不等于服务器上线或 production acceptance。

## 目标服务器准备

先确认 `RELEASE_MANIFEST.json` 固定的 OCR/Qdrant tag 在服务器存在，再校验外层上传
归档 sidecar 并解压到新目录。`preflight.sh` 与 `install.sh` 都会执行完整 image ID、
`linux/amd64` 平台和 revision 复核；不要重新 tag 来绕过失败。复制
`.env.example` 到包外的私有绝对路径，使用 `generate-secrets.sh` 生成四个互异 secret，
再填写 release ID、模型 endpoint、OCR mode 和 GPU ID。私有 env 不得放回 release 包。
刚解压时先执行以下无副作用校验：

```bash
bash preflight.sh --package-only
bash verify.sh --package-only
```

独立 OCR：

```text
RAG_OCR_MODE=dedicated
RAG_OCR_ENDPOINTS=["http://rag-industry-ocr:8090"]
RAG_INDUSTRY_OCR_GPU_DEVICE_ID=<明确未占用 GPU>
```

外部 OCR：

```text
RAG_OCR_MODE=external
RAG_OCR_ENDPOINTS=["http://VERIFIED_OCR_ENDPOINT"]
```

外部模式只有 `/ready` 和最小 endpoint 合同通过后才能继续，不能通过读取培训 OCR
volume 或 secret 来共享。

## 首次部署顺序

以下命令中的路径必须替换为服务器真实绝对路径：

```bash
bash preflight.sh /ABSOLUTE/PRIVATE/rag-industry.env /ABSOLUTE/RELEASE_DIR
bash install.sh /ABSOLUTE/PRIVATE/rag-industry.env /ABSOLUTE/RELEASE_DIR
bash deploy.sh /ABSOLUTE/PRIVATE/rag-industry.env /ABSOLUTE/RELEASE_DIR
bash run-index.sh /ABSOLUTE/PRIVATE/rag-industry.env
bash verify.sh /ABSOLUTE/PRIVATE/rag-industry.env
```

`deploy.sh` 只启动工业 Qdrant、可选 OCR 和 app，不自动建索引。首次必须显式运行
`run-index.sh`，它通过 `POST /api/index/jobs` 创建 `full` job，只启动一次
`rag-industry-worker`，随后核验 `rag-industry-active`、active manifest、10 个 source、
snapshot 和非零 point count。不得用 incremental 替代首次 full。

首次建索引前 `/live` 应成功；尚无 active manifest 时 `/ready` 可以按现有合同失败。
full index 完成后 `/ready` 必须成功。

## 回滚和隔离边界

```bash
bash rollback.sh /ABSOLUTE/BACKUP/previous.env
```

脚本始终显式使用 `-p rag-industry` 和 Industry Compose，只恢复上一套 Industry app、
corpus/config 路径，不执行通配容器删除，不调用 training project。每次 full index 前会
在 Industry backup 目录保存 active manifest SQLite 快照和 alias 目标；若当前容器 revision
与回滚描述一致，`rollback.sh` 会在停止 Industry app 后同时恢复旧 alias 与 manifest
state。索引发布失败会保留旧 Industry alias；失败 release、任务报告和快照保留用于审计。

Serving App Update 不使用上述首次部署 rollback 参数，而使用更新包安装后的版本化脚本：

```bash
bash /ABSOLUTE/SERVING_RUNTIME/rollback-app-update.sh \
  /ABSOLUTE/PRIVATE/rag-industry.env \
  /ABSOLUTE/UPDATE_BACKUP
```

rollback 只读取经 SHA 和 exact-set 验证的 last-good 原子 pointer；必要时在停止失败的新
App 后恢复 Trace 在线备份，再使用旧 env/Compose/config/image 重建旧 App。它不得恢复
未被本次更新修改的 index state，也不得触碰 OCR、Qdrant 或 worker。

培训与工业前端切换时必须创建新的 `conversation_id`。当前没有 `kb_id` 自动路由，也
不会同时查询两个知识库后再选择分数较高的结果。

## 传输

### 首次部署传输

首次部署才使用 `artifacts/industry-upload` 下的外层 `.tar.gz` 和同名 `.sha256`，并按
首次部署包内 `SERVER_UPLOAD_COMMANDS.txt` 操作。每一跳都先传 sidecar，再执行
`sha256sum -c`；不要上传仓库、原始 `.doc`、私有 env、服务器密码或本地
LibreOffice 转换器镜像。

### Serving update 传输

Serving update 不得引用 `artifacts/industry-upload`。只上传新的
`artifacts/industry-serving-update/<new-sha12>/` 八文件 exact set，或者先在该目录外
生成受控外层归档及同名 SHA256 sidecar。中转和目标机都使用既定的
`/data/tyf/RAG/industry-transfer`，从 fresh shell 以部署用户逐跳传输并校验；主机名
使用现场批准的 `<BASTION_HOST>` 与 `<TARGET_HOST>` 占位，不写进交付包。

目标机解包到新目录后，严格按包内 `SERVER_UPDATE_COMMANDS.txt`：校验两个 sidecar、
执行 package selfcheck、以绝对路径传入私有 env、运行 updater，最后用 Python
标准库读取 audit root 下各 attempt 的 `transaction-state.json`。不要 source env，
不要运行 `run-index.sh`，不要手工删除失败 attempt；`verified` 表示本次更新本地终验
完成，`rolled_back` 表示已恢复旧 App，其他终态一律停止并复核。
