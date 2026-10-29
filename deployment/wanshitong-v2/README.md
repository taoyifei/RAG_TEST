# 湾小度 V2 测试部署

本目录仅面向 `54:8289 → 60:8289`。Compose 项目为 `wxd_v2_083_8289`，数据根目录为 `/srv/wanshitong-data/wxd-v2-test`。不要将这里的网关、原生引擎或数据卷接到生产 8288；旧 `candidate` 名称不代表可停止的测试资源。

## 固定版本与运行时

上游版本固定为 WeKnora `v0.8.3`，commit `3e22822c1d2397be730c458c7539d0eb8c113a24`。构建所需上游源码保留在 `vendor/weknora`（不发布官网、演示视频等资料；来源见根目录上游锁），Vue 仅重放湾小度品牌、网关认证和管理导航补丁。原生 Go/Python 后端没有本地业务补丁。

测试引擎采用有明确来源的083派生镜像：

- app 在固定082运行依赖镜像上安装官方083二进制、完整083 config/scripts/migrations 和许可证。官方二进制层 SHA-256 为 `e204c56564022f24c19ecfd7c3166e827ed3a5dc5262954e9946dc1e82c627aa`，解包后二进制 SHA-256 为 `e6ed5baa4e08f09d3963b1421690a80917c9220a18301b7eda2ff61b9c3eb383`。
- docreader 在固定082依赖镜像上替换完整083解析源码、启动入口和依赖锁文件。082与083的 `docreader/pyproject.toml`、`uv.lock` 完全相同；Dockerfile差异为浏览器下载镜像参数及固定venv启动入口，不需要在线安装新依赖。
- app runtime Dockerfile的变化仅位于构建浏览器组件时的镜像/CA引导，运行时段无变化；DuckDB Go及bindings版本也相同。官方083二进制的动态依赖只有 libm/libstdc++/libgcc/libc/ld-linux，最高GLIBC符号为2.35，兼容固定Bookworm运行时。

这两个派生镜像应记录实际构建的不可变镜像ID，不能标为官方完整镜像digest。官方083镜像清单、源码归档校验值和锁文件校验值见 `upstream-lock.json`。

## 离线构建

先将本地的 `build-context/native-app`、`build-context/vue`、`build-context/react`、`build-context/wheels` 放到本目录，源码目录保持与仓库相同结构。以下命令从源码根目录执行：

```sh
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.app-083 --build-arg WXD_APP_RUNTIME_IMAGE=sha256:bc8a534799fcb54045100daf4bc5db86270b38ae45c5f761c410c50b4dcd6cdc -t wxd-weknora-app:0.8.3-3e22822 .
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.docreader-083 --build-arg WXD_DOCREADER_RUNTIME_IMAGE=sha256:3c36e7e738ba515315523876d8041f9bb02809145a4e3b331e2550e4bff98b7a -t wxd-weknora-docreader:0.8.3-3e22822 .
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.gateway --build-arg WST_GATEWAY_RUNTIME_IMAGE=<已核对的网关运行镜像ID> -t wxd-gateway:v2 .
docker build --pull=false --network=none -f deployment/wanshitong-v2/Dockerfile.edge --build-arg WST_EDGE_RUNTIME_IMAGE=<已核对的Nginx运行镜像ID> -t wxd-edge:v2 deployment/wanshitong-v2
```

`.env.example` 不包含凭据。复制为本次测试自己的 `.env` 后填写现场值及构建镜像ID。SSO注册表只含 `id/origin/authorize_url`，回调由网关自动推导；使用测试 `wanshitong_dev` client，Cookie部署ID为 `wxd_v2_test_8289`。管理员用户名配置为 `WANSHITONG_ADMIN_USERNAMES=taoyifei`。

四个测试网络固定为 front `192.168.80.0/24`、engine `192.168.81.0/24`、data `192.168.82.0/24`、model-egress `192.168.83.0/24`。这些网段在部署前已核对无占用；edge使用 `192.168.80.3`，网关只信任该代理。

## 模型与图片

复用现有模型API，不新建GPU服务。LLM地址为 `http://10.242.180.54:18000/v1`，embedding为Docker宿主8091，rerank经独立adapter转到宿主8092。模型白名单必须包含54。

OCR通过本目录 `ocr_relay.py` 的固定上游转发复用现有OCR VLM。它不加入任何生产容器网络，只监听宿主内部 `172.17.0.1:18095`；`WXD_OCR_UPSTREAM` 必须通过只读inspect取得当前模型IP后填写。原生OCR模型配置使用 `http://host.docker.internal:18095/v1`。

DOCX解析器会抽取并保存嵌入图片；Go ImageResolver保存资源发生在OCR异步任务之前。图片抽取/显示不依赖OCR，检索仅存在于图片中的文字仍需要OCR。不能因为文档文本可检索便认定图片已正确显示，部署验收还须检查具体图片资源和回答引用。

## 已完成的本地验证

- Vue `npm ci`（固定上游lock）、`build-with-types`通过，完整源码可以构建ArtifactLibrary及新的PDF预览。
- 既有管理跳转功能测试2项通过。
- OCR relay在回环端口的health、模型列表、聊天转发实际HTTP smoke通过。

这些本地结果不等同于真实8289登录、解析、检索和图片验收；现场验证及生产基线对照由主执行流程记录。
