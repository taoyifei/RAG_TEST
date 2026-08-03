# 阻塞项

本文件保留当前目标明确要求继续保留的六类外部阻塞，以及真实 smoke 新发现且
超出本轮授权范围的安全解包权限阻塞。历史审计、已解决环境问题和不再阻塞本目标
的职责说明均已移至 `PROGRESS.md`。

## 置顶：安全解包器丢失 runtime 执行权限

- 稳定错误码：`FRESH_VERIFY_FAILED`；真实 smoke 第 1 轮前 9 阶段通过后，
  `verify-offline.sh` 报 `runtime bootstrap.sh 不可执行`，release-safety 尚未执行，
  handoff 未生成，失败报告没有可交付 `release_dir`。
- 源 bootstrap 为 755/Git 100755，runtime tar entry 也保留执行位；既有
  `scripts/offline_bundle.py::_extract_regular_members` 只复制普通文件字节且没有
  恢复任何 mode，导致其“安全解包”结果统一丢失执行位。服务器部署说明使用同一
  解包器后立即运行 runtime verifier，因此在 `release_smoke.py` 内额外 chmod 会
  掩盖真实交付缺陷，禁止用该方式刷绿。
- 本轮授权只允许修改 release-safety、release-smoke 及对应测试，不允许修改
  `scripts/offline_bundle.py` 和对应解包契约测试。解除条件：另行授权让安全解包器
  只恢复 manifest 已登记普通文件的受控权限（至少精确恢复批准的 runtime 可执行
  文件），新增恶意 mode/symlink/特殊文件反测，并从最新 clean HEAD 重跑全部门禁
  与真实 smoke。

## 1. 真实模型契约

- 当前任务禁止访问 `.57/.58/.60`，因此没有 embedding、reranker 和四个 LLM
  的真实 health/models、revision、schema、维度、上下文上限与最小请求报告。
- `deployment/config/pipeline.json` 中生产模型 revision 继续保持
  `pending-server-verification`；smoke 包只携带模型契约验证器，不伪造报告。
- 解除条件：在目标网络对六个端点分别运行
  `scripts/verify_model_contracts.py`，每份脱敏 JSON 均为 `status=passed`，且由
  独立部署记录绑定 endpoint、model/tokenizer/code revision 与服务版本。

## 2. retrieval 定参与冻结

- `deployment/config/retrieval.json` 继续为 `provisional`，没有使用真实模型与
  人工冻结集完成 tuning/holdout，因此不能改为 frozen 或使生产 ready。
- 当前配置 SHA256 为
  `7f3d27750d5a5129bf26357fcb1627cbf389d9671f4c3118f765896fae2c1bd5`；配置完整性
  测试已改为从 `deployment/ASSETS.sha256` 读取唯一摘要并核对实际文件，继续
  断言 `status=provisional` 与 `freeze_decision_sha256=null`。该修复只消除重复
  摘要来源，不代表 retrieval 已完成定参或冻结。
- 解除条件：核验真实模型契约后，用 tuning 集确定参数、独立 holdout 验收并生成
  `FREEZE_DECISION`；届时由获授权任务原子更新配置及其冻结契约。

## 3. GPU OCR 实测

- 当前 HEAD 的 OCR 镜像已在本地以 `--network none` 实际构建、自检并验证为
  `linux/amd64`，revision 精确等于
  `d7d2546f51d912be0cb0025757922d770f05d833`。
- 本任务禁止访问服务器，因而没有 `.60` NVIDIA runtime、GPU index/显存、
  `/ready`、真实图片请求、耗时、OOM 或重启证据；本地镜像成功不能替代 GPU 验收。
- 解除条件：服务器只读 preflight 通过后，在获授权窗口完成 GPU OCR 启动、
  真实请求、资源与故障恢复验收，并保留脱敏输出。

## 4. EMF 转换器

- 18 个 EMF 引用尚无经过许可证、安全和离线分发审计的固定转换器资产；当前实现
  必须明确返回 `EMF_RASTERIZER_UNAVAILABLE`，不得猜测或静默计为 OCR 成功。
- 解除条件：选择固定版本，冻结二进制 SHA256/许可证和命令契约，完成畸形输入、
  文件/CPU/内存/超时上限反测后再更新 parser/OCR revision。

## 5. Word 自动编号

- 真实 DOCX 中有 268 个 `list_level` 非空段落；当前 parser 读取 runs 与列表层级，
  不解析 `numbering.xml` 渲染自动编号，编号 marker 不能作为可验证引用原文。
- 本任务禁止修改 Parser；不能用猜测编号填充证据。
- 解除条件：另行实现只读 numbering renderer，覆盖多级编号、restart、style 继承
  和缺失定义反测，更新 parser revision 后重建索引并复核 6 个 DOCX。

## 6. production 验收

- 本轮只完成本地 smoke：app/OCR 实际构建、断网自检、Qdrant 归档、13.34GB
  runtime 与 21.95MB corpus 双包、七文件 sidecar 和全新目录解包验证均通过。
- 未 SSH/SCP、未访问或部署 `.60`，也没有服务器 `docker load`、GPU、真实模型、
  6/6 入库、50 题指标、10 万 chunk、5 并发、备份/回滚与生产 ready 证据。
- 解除条件：前五类依赖满足后，在获授权服务器窗口执行 SHA 校验、离线 load、
  `compose up --no-build --pull never`、全新卷启动、完整质量/性能/故障与回滚验收。
