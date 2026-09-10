# Word 文档 RAG

这是一个可追溯、可增量更新、可恢复的 DOC/DOCX 检索增强生成产品。默认
`rag-app serve` 提供中文管理控制台和稳定 API；V1 使用 SQLite 保存产品状态，
使用正式 Qdrant Server 保存向量。Jina 与阿里云百炼凭据由管理员在页面配置，
不会写入镜像或浏览器存储。

## 五分钟容器路径

首次拉取基础镜像和构建时间不计入五分钟操作路径。默认只启动 `app` 与
`qdrant`，应用端口只绑定宿主 loopback：

```bash
cp .env.example .env
python scripts/release.py build
docker compose run --rm --no-deps app \
  init-secrets --directory /run/rag-secrets
docker compose up -d --no-build --pull never
docker compose ps
```

`release.py build` 只接受干净的已提交 HEAD，从 Git 对象按 Dockerfile 必需
白名单导出仓库外 BuildKit context，并在 `artifacts/release-context/` 保存不含
正文 Secret 的文件摘要、权限和 source SHA manifest。根 Compose 只消费已构建
镜像，不再递归发送工作树；修改源码后须重新提交并重建候选。

初始化命令排他创建 0600 主密钥、Bootstrap Token、Qdrant API Key 与配置。
先把 `rag_secrets` 卷和主密钥单独备份，再用下面的命令在当前终端读取一次
Bootstrap Token：

```bash
docker compose run --rm --no-deps --entrypoint sh app \
  -c 'cat /run/rag-secrets/admin-bootstrap-token'
```

打开 `http://127.0.0.1:8088/`，输入 Bootstrap Token。随后在“模型服务”依次
保存 Jina 与阿里云百炼连接。百炼须显式选择端点模式，业务空间模式使用控制台实际 API Host。完成出网与预算授权后测试连接，创建项目和知识库、激活主备检索方案，上传
DOC 或 DOCX 后即可问答。DOCX 直接进入当前 OOXML Parser；旧版 DOC 在隔离
转换器可用时先转为清洗后的派生 DOCX，再进入同一表格、媒体、Artifact 与 Chunk
链。只有转换器不可用时才显式降级为 antiword 纯文本，并记录结构降级。发送到远程 Provider 的只有管理员明确授权的查询、文档切片
和重排候选；页面会显示操作、Token 与切换用量。没有凭据时仍可完成本地 Exact/FTS
检索，但不得把它称为 Live Ready。

完整步骤见 `docs/public/quickstart.md`，部署与 TLS 见
`docs/public/deployment.md`，数据出网边界见
`docs/public/data-egress-and-cost.md`。

## 回答、图片识别与历史

在“模型服务”保存百炼连接后，进入知识库的“知识库模型”选择回答模型、
一次按需问题改写和图片文字识别模型。远程调用仍需要与该知识库、来源和模型
绑定的有效预算授权；不会借用公开合成验收的额度。默认未配置时使用证据摘录，
配置后以有限来源片段生成回答，并校验引用、对象、数字与否定；模型失败时明确
显示摘录回退及原因。回答和改写配置只改变服务及缓存身份。

文档列表分别展示登记状态和当前索引可检索性。任务失败可查看安全原因，
无当前版本的失败文档也可逻辑删除；删除立即隔离新查询、缓存和原文下载。
原件重试沿用逻辑文档，新索引验证完成后才切换活动版本。

DOCX 原生文字与表格优先。文档“图片识别”先扫描内嵌媒体，再选择已经获准的
图片运行 OCR；缓存命中不重复调用。未完成或不支持的媒体会单独列出，保留可用
原生文字。OCR 新增文本进入新索引版本，引用标记为“图片识别文字”并可打开
受权限保护的原图；未提供的坐标或置信度不会补造。当前不支持 EMF 转换。

“问答历史”按时间、知识库、结果和正文关键字查找请求，并用既有主密钥加密
问题及答案，默认保留七天；“Operational Trace”在独立数据库中只保存安全身份、
时序、候选决定和显式调试制品。管理员可下载单条或批量 History + Trace 支持包；
包含问答与引用正文必须显式确认并在下载时重新鉴权。清理问答历史只删除 History
和旧平面事件，不删除独立 Operational Trace。

问答页使用绑定 owner、Project、KB 的有界会话，只在唯一成功 final 后保存问题和
已验证事实摘要；支持有用/无用及有限原因码反馈。非流式 SAFE 等价请求可共享底层
singleflight 计算，但每个请求仍有独立 Trace 和 History；流式、DIAGNOSTIC、FULL
及跨 owner/scope/Revision/会话请求不会合并。进程内结果缓存采用 TTL、LRU、512
条和约 64 MiB 双重上限，不新增明文持久缓存。成功、拒答、缓存命中及模型失败均
显示实际调用与原因，重启时未结束的请求标记为中断。历史访问重新检查来源与权限，
删除来源后正文不再可见。浏览器断开连接不保证取消已经开始的不可中断模型请求。
通过 `RAG_HISTORY_SAVE_BODY=false` 关闭新请求正文保存，或用
`RAG_HISTORY_RETENTION_DAYS` 设置 1–365 天保留期；页面提供清理历史入口。
完整隐私、导出、会话与清理合同见
[History 与 Operational Trace](docs/public/history-trace-conversation.md)。

## 发布验收

Python 3.11 虚拟环境安装 `requirements.lock` 后，根目录只需这些主入口：

```bash
python scripts/dev.py check
python scripts/dev.py smoke
python scripts/dev.py product-check
python scripts/dev.py web-e2e
python scripts/release.py build
python scripts/release.py verify
python scripts/release.py acceptance
```

默认验证离线且无 Key。真实验收支持既有本地有界续跑与受保护的
`P11 Live Provider` Workflow；两者都要求明确授权、持久 campaign、累计预算
和公开合成语料边界。Workflow 的授权短语与 Environment 审批不代替预算批准。
先执行零调用配置诊断，前置条件具备后才运行获准的 Live 步骤：

```bash
python scripts/release.py acceptance --resume --steps config_check \
  --container <目标App> --config <本地非秘密配置> --exit-scope selected
python scripts/release.py budget-plan --config <本地非秘密配置> \
  --container <目标App> --budget-history <已核对的累计账> \
  --plan-output release/p11-budget-plan.json
```

预算配置复用 `source_profile_revision_id`，允许合法未激活草稿；无方案时仅输出
`actual_profile_bound=false` 的默认假设，不能直接批准。审批前与执行前重新生成并
核对方案身份，变化则计划失效、历史用量保留。所选步骤退出码为 0 不等于 P11_READY。
完整步骤与当前未决条件见 [P11 最终验收](docs/progress/p11-final-acceptance.md)
和 [当前验收](release/p11-repair-acceptance.md)。

## 主要组成

- `src/rag_app/`：安全 DOC/DOCX 解析、产品状态、检索、引用、备份与 API。
- `frontend/`：React/TypeScript 中文控制台与 Playwright 测试。
- `compose.yaml` 与 `Dockerfile`：V1 默认的简单容器路径。
- `evaluation/`：独立评测 schema、证据校验和指标计算。
- `deployment/`：只为迁移保留的旧 Industry/OCR 部署资产。

## 备份与数据生命周期

停止写流量和索引任务后，使用已有 Product 命令创建并校验备份：

```bash
docker compose exec app rag-app backup create --data-dir /data \
  --output /data/backups/p11-backup.tar.gz \
  --compatibility-manifest /app/compatibility-manifest.json
docker compose exec app rag-app backup verify \
  --archive /data/backups/p11-backup.tar.gz
```

备份不包含主密钥等 Secret，需独立保存。恢复使用新的空数据目录与独立 Qdrant，
具体参数见 [备份与恢复](docs/public/backup-restore.md)。当前 Product 没有独立
GC CLI；旧 `index-gc` 面向 Legacy 数据库，不能用于产品数据卷。

登录与权限见 [产品安全](docs/public/security.md)；DOCX 与旧 DOC 支持边界见
[结构支持矩阵](docs/public/docx-support-matrix.md)。旧 DOC 的派生结构来自转换实例，
不能证明原二进制排版；转换器不可用时的 antiword fallback 不保留表格、图片、
页眉页脚、批注或修订结构。

旧 `RAG_RELEASE_REVISION`、tokenizer/pipeline/retrieval JSON、debug/admin token、
GC 与七文件部署说明完整保留于 [Legacy 运行指引](docs/public/legacy-runtime.md)，
迁移见 [Industry 迁移](docs/migration/from-industry.md)。
