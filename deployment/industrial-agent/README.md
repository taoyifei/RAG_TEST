# 工业智能体部署

组件包括用户与管理前端、Python 网关、Go 知识引擎、文档解析服务、PostgreSQL 和 Redis。默认入口为 `http://127.0.0.1:18300/kb/`，网关和引擎的端口留在容器网络中。

## 配置与构建

在本目录复制 `.env.example` 为 `.env`。为该实例选择独立的 `IA_DATA_ROOT`、`IA_SECRET_DIR` 和 `IA_DEPLOYMENT_ID`。身份服务需要登记 `/kb/sso/callback`，站点入口和 SSO Origin 必须一致。管理员账号和超级管理员 ID 通过 `IA_ADMIN_USERS` 与 `IA_SUPER_ADMIN_USER_IDS` 显式配置。

复制 `config/builtin_models.example.yaml` 到部署目录外，配置对话、向量和重排服务，在 `IA_MODELS_FILE` 指向实际文件。保持脚本要求的模型 ID 与配置文件一致，向量维度和上下文长度采用模型服务的实际值。

已准备离线镜像时，可通过 `IA_APP_IMAGE`、`IA_DOCREADER_IMAGE`、`IA_GATEWAY_IMAGE` 和 `IA_EDGE_IMAGE` 指定本地镜像，运行时使用 `--no-build`。

```sh
docker compose config --quiet
docker compose build app docreader gateway edge
```

如需在本机单独构建管理端，从仓库根目录进入管理前端后执行。

```sh
cd vendor/knowledge-engine/frontend
npm ci
VITE_INDUSTRIAL_AGENT_GATEWAY_AUTH=true npm run build
```

## 初始化与启动

1. 在数据根目录准备 `postgres`、`redis`、`files`、`docreader-tmp`、`gateway` 五个子目录。确保容器可读写相应目录，网关的 `gateway` 使用 UID 和 GID 10001。
2. 在 Secret 目录准备 `db_password`、`redis_password`、`jwt_secret`、`system_aes_key`、`system_signing_key`、`gateway_master_key`、`gateway_admin_bootstrap_token`、`sso_client_secret`、`external_signing_key` 和 `knowledgeengine_admin_password`。AES 密钥是 32 字节 ASCII 文本，网关主密钥可用 `openssl rand -base64 32` 生成。SSO 密钥由身份服务提供。网关使用的 Secret 应由 UID 10001 读取。
3. 用 `IA_DISABLE_REGISTRATION=false docker compose up -d app` 先运行引擎与依赖，临时允许独立服务账号注册。
4. 在当前项目的 `<项目名>_engine` 网络内运行 `bootstrap_workspace.py`。在临时 Python 容器中挂载该脚本和 Secret 目录，脚本默认连接 `http://app:8080`。它创建 `ia-admin@internal.test` 和公共知识库，并将限定范围的 Key 写入 `workspace_api_key`。将输出的 `tenant_id` 与 `public_kb_id` 分别填入 `IA_ENGINE_TENANT_ID` 与 `IA_PUBLIC_KB_IDS`。
5. 恢复 `IA_DISABLE_REGISTRATION=true`，运行 `docker compose up -d --no-build`。登录后在管理后台保存公共问答配置，上传资料，待解析完成后验证问答与附件引用。

已有图像服务可通过 `ocr` profile 接入。完整布局解析可使用 `compose.layout-ocr.yaml`，在 `IA_LAYOUT_OCR_NETWORK` 和 `IA_LAYOUT_OCR_CONF` 提供服务网络与转发配置，参考 `layout-ocr-relay.conf.example`。工作助手默认关闭。
