# 湾小度 V2 测试部署

本目录仅面向 `54:8289 → 60:8289`。0.9.0项目为 `wxd_v2_090_8289`，数据根目录为 `/srv/wanshitong-data/wxd-v2-test-090`；2026-11-01 已在临时18299端口完成预演并切换到8289，18299不再作为入口。现场固定镜像与配置位于 `/data/tyf/wxd-v2-090-20261031/compose.json`。生产继续使用独立 `wxd_prod_083_8288` 及其0.8.3数据，不使用本分支更新。

## 固定版本与运行时

上游固定为 WeKnora `v0.9.0`，commit `f17ee8a8da901bd658a4cb25f40b5fd41adbfdd7`。业务源码保留在 `vendor/weknora`，官网和演示视频继续裁剪。Vue重放品牌、网关认证和管理导航补丁，管理端仍关闭原生助手、自动化及工作区默认模型入口；只读员工工作助手使用单独的 `workbench.html`。原生Go与Docreader源码没有本地业务补丁。

测试引擎使用经摘要核验的官方产物：

- app直接加载完整官方090 amd64镜像，逐层验证压缩SHA256和解压diff_id，并比对配置、架构和18层RootFS。官方manifest为 `sha256:0497efa4dbe365e0f99d25fa386c9034346f095a95facddf2fade02edc16bccd`，原始config为 `sha256:902186be9c292b3b3ea8080d0a8dbaeea59b62923473c7cb04df0c53386dd50b`。
- Docreader从官方090镜像取得完整venv、解析源码和锁文件，整体替换旧venv，避免混用旧依赖。其Python3.10.18与前8个基底层和083一致，复用已固定Bookworm运行时；不是声称082与090依赖锁相同。
- 新解析依赖包含 RapidOCR 3.10.0、OpenCV headless 5.0.0.93；NumPy 2.2.6和ONNX Runtime 1.20.1保持兼容。实际容器须验证模块导入及文件解析，不能仅凭静态ABI分析认定可用。

记录实际Docker导入/构建ID，不能把daemon报告的ID或派生reader ID写成官方registry manifest。官方来源、源码归档及依赖摘要见 `upstream-lock.json`；现场实际镜像以发布目录清单为准。

## 离线构建

先核对离线包摘要，加载官方app，并将reader包的 `app/` 放到 `build-context/docreader/app/`；Vue、React产物及固定multipart wheel分别放到其对应build-context子目录。管理端沿用[网关构建说明](../../vendor/weknora/frontend/WANSHITONG_GATEWAY.md)，不可使用官方默认登录构建。以下命令从源码根目录执行：

```sh
docker load --input weknora-app-090.docker.tar.gz
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.docreader-090 --build-arg WXD_DOCREADER_RUNTIME_IMAGE=sha256:3c36e7e738ba515315523876d8041f9bb02809145a4e3b331e2550e4bff98b7a -t wxd-weknora-docreader:0.9.0-f17ee8a .
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.gateway --build-arg WST_GATEWAY_RUNTIME_IMAGE=<已核对的网关运行镜像ID> -t wxd-gateway:v2 .
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.edge --build-arg WST_EDGE_RUNTIME_IMAGE=<已核对的Nginx运行镜像ID> -t wxd-edge:v2 deployment/wanshitong-v2
```

`.env.example` 不包含凭据。复制为本次测试自己的 `.env` 后填写现场值及构建镜像ID。SSO注册表只含 `id/origin/authorize_url`，回调由网关自动推导；使用测试 `wanshitong_dev` client，Cookie部署ID为 `wxd_v2_test_8289`。

超级管理员通过 `WXD_SUPER_ADMIN_USER_IDS=325` 绑定研发系统稳定用户ID，Compose将其传给网关的 `WANSHITONG_SUPER_ADMIN_USER_IDS`。页面只能修改普通、人力、管理员三种业务角色，不能授予或撤销超管身份。管理员任免与同级保护规则见根目录README；角色变更记录保存在测试网关数据库 `user_role_changes` 表中。

四个090测试网络为front `192.168.88.0/24`、engine `192.168.89.0/24`、data `192.168.90.0/24`、model-egress `192.168.91.0/24`。部署前核对无重叠；edge固定 `192.168.88.3`，网关只信任该代理。不得覆盖生产84–87或旧测试80–83网段。

已有8289升级须完整备份PostgreSQL、网关SQLite、files、Redis和docreader临时文件，保留测试SSO密钥、部署ID、原生用户、角色、个人资料及历史。候选恢复到自己的卷，执行118→134迁移；网关SQLite不运行WeKnora Lite的SQLite迁移。切换前再次停写同步，保留旧测试栈用于回退；迁移125的数据清理不能靠down恢复。

## 模型与图片

复用现有模型API，不新建GPU服务。LLM地址为 `http://10.242.180.54:18000/v1`，embedding为Docker宿主8091，rerank经独立adapter转到宿主8092。模型白名单必须包含54。

OCR通过本目录 `ocr_relay.py` 复用现有OCR VLM，不部署新GPU模型。090 relay仅监听宿主内部 `172.17.0.1:18097`；上游通过只读inspect核对。仅更新候选自己的OCR模型地址为 `http://host.docker.internal:18097/v1`，不改生产18096或旧测试18095。

DOCX解析器会抽取并保存嵌入图片；Go ImageResolver保存资源发生在OCR异步任务之前。图片抽取/显示不依赖OCR，检索仅存在于图片中的文字仍需要OCR。不能因为文档文本可检索便认定图片已正确显示，部署验收还须检查具体图片资源和回答引用。

## 兼容与验收

- 消息历史接口单次上限200；网关按完整时间戳组分页，不把服务端裁剪误判为结束。满200条同刻消息无法由原生时间游标安全遍历时明确报错。
- 定向接入图片管线目录及参数校验；知识库设置保存仍经原有资料权限检查。员工工作助手只提供本人独立空间的定向代理，不提供通用原生接口；融合边界见[方案](../../docs/WEKNORA-090-INTEGRATION.md)。
- 本轮网关/身份280项、用户端133项、Vue相关160项和完整类型构建通过；46个网关模块严格mypy及源代码ruff通过。它们不替代真实登录、资料解析、图片和跨角色验收。

## 工作助手初始化与启用

默认 `WXD_WORKBENCH_ENABLED=false`。只有目标测试服务账号完成原生SystemAdmin引导、原生内置模型可用于新空间、模型工具调用和真实端到端验收通过后，才把该开关设为true并重建测试gateway。生产配置不改动。

- `WXD_NATIVE_BOOTSTRAP_ADMIN_EMAIL` 使用测试服务账号邮箱；这是服务端创建原生普通用户和独立空间的权限，不能把人类管理员直接设置为原生平台管理员。网关不开放平台管理接口。
- `WXD_NATIVE_WORKBENCH_PROVISIONING=true` 仅供服务端创建空间；浏览器不能创建或切换任意tenant。按规模设置 `WXD_NATIVE_MAX_OWNED_WORKSPACES`，本次测试为1000。
- `WXD_WORKBENCH_CHAT_MODEL_ID` 可显式选择已授权且可供独立空间使用的模型。未填时选择原生可用KnowledgeQA模型；现有模型仍由现场固定配置管理。
- 运行模型必须支持 `tool_choice=auto`。54共享vLLM已按用户明确授权启用自动工具选择和hermes解析器，模型工具调用及8288实际问答通过；但真实原生助手仍超出8192上下文。24576方案已完成只读容量和配置核对，尚待额外维护授权；不能只把原生模型元数据写大来掩盖真实服务上限。
- 构建Vue时须保留两个HTML入口，edge同时发布React、管理端和工作助手产物；Nginx将 `/kb/workbench/{id}/` 映射到工作助手HTML，API仍经Cookie/CSRF网关。
- 工作助手按“研发用户 + 资料范围 + 代次”隔离，SQLite和原生PG需一起备份。不得只删SQLite工作区表或恢复旧表后继续运行原生定时任务。

关闭入口可立即把开关设为false；如已有运行中的后台任务，须先按工作助手服务撤销相应空间和任务，不能把隐藏UI当作停止任务。回退网关/edge可使用发布前镜像与配置，原生数据保持090，不执行降级迁移。

真实部署及生产只读基线对照记录在[实施记录](../../docs/V2-8289-IMPLEMENTATION.md)。私密快照、原件、Cookie、Trace、镜像和过程包不进入Git。
