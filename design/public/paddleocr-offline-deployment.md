# PaddleOCR 3.5.0 离线 GPU 部署

## 固定基线

- OCR：PaddleOCR 3.5.0。
- 模型：`PP-OCRv5_server_det` 与 `PP-OCRv5_server_rec` 本地静态模型。
- 引擎：`paddle_static`（代码参数 `engine="paddle"`）。
- 设备：单张 GPU，容器内固定 `gpu:0`；并发上限 1。
- 禁用：VL、HPI、TensorRT、文档方向分类、去扭曲和文本行方向模型。
- 基础镜像：
  `paddlepaddle/paddle:3.3.0-gpu-cuda12.6-cudnn9.5@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776`。
  官方镜像元数据显示它是 linux/amd64、Ubuntu 22.04、Python 3.10、
  CUDA 12.6.3 和 cuDNN 9.5.1.17。

主应用继续使用 Python 3.11。OCR 镜像只复制独立 `rag_app/ocr` 源码，以免为
迁就基础镜像而放宽主工程 Python 约束。

## 1. WSL 下载与校验

从仓库根目录执行：

```bash
.venv/bin/python scripts/download_ocr_assets.py
.venv/bin/python scripts/download_ocr_wheels.py
(cd deployment/ocr/assets && sha256sum --check MANIFEST.sha256)
(cd deployment/ocr/assets/wheelhouse && \
  sha256sum --check ../../WHEELS.sha256)
```

模型脚本只允许 Paddle 官方 HTTPS 主机，限制 tar 成员数和解压总量，拒绝
路径越界、链接、特殊文件和不一致覆盖。wheel 脚本固定 PyPI、CPython 3.10、
linux/amd64、59 个确切文件及 SHA256，重复执行只做集合与摘要校验。

## 2. 本地断网构建

先在允许联网的构建机拉取固定基础镜像；这一操作绝不能在目标服务器执行：

```bash
docker pull \
  paddlepaddle/paddle:3.3.0-gpu-cuda12.6-cudnn9.5@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776
docker image inspect \
  paddlepaddle/paddle:3.3.0-gpu-cuda12.6-cudnn9.5@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776
```

随后强制断网构建。Dockerfile 只使用本地 wheels、模型和许可证：

```bash
docker buildx build \
  --network none \
  --platform linux/amd64 \
  --file deployment/ocr/Dockerfile \
  --tag docx-rag-ocr:0.1.0 \
  --load \
  .
docker image inspect \
  --format '{{.Os}}/{{.Architecture}} {{.Id}}' \
  docx-rag-ocr:0.1.0
```

## 3. 断网、GPU 与 API 冒烟

准备一张不含敏感信息的 PNG，生成至少 32 字符的临时 token：

```bash
export RAG_OCR_SMOKE_TOKEN="$(openssl rand -hex 32)"
docker run -d \
  --rm \
  --name rag-ocr-smoke \
  --network none \
  --gpus 'device=0' \
  --read-only \
  --tmpfs /tmp:size=512m,mode=1777 \
  --mount type=bind,src="$PWD/smoke.png",dst=/smoke.png,readonly \
  --env RAG_OCR_API_TOKEN="${RAG_OCR_SMOKE_TOKEN}" \
  docx-rag-ocr:0.1.0
docker exec rag-ocr-smoke python -c \
  "import paddle; print(paddle.__version__, paddle.device.cuda.device_count())"
docker exec rag-ocr-smoke python -c \
  "import urllib.request; print(urllib.request.urlopen(
  'http://127.0.0.1:8090/ready', timeout=3).read().decode())"
```

上面完成运行时断网与 GPU 加载证明后，停止临时容器。若还要人工核对 curl
契约，可在隔离构建机另起一个只映射 loopback 的临时容器；不要把 OCR 端口
加入生产 Compose：

```bash
docker stop rag-ocr-smoke
docker run -d \
  --rm \
  --name rag-ocr-curl \
  --gpus 'device=0' \
  --read-only \
  --tmpfs /tmp:size=512m,mode=1777 \
  --publish 127.0.0.1:18090:8090 \
  --env RAG_OCR_API_TOKEN="${RAG_OCR_SMOKE_TOKEN}" \
  docx-rag-ocr:0.1.0
media_sha256="$(sha256sum smoke.png | awk '{print $1}')"
content_base64="$(base64 -w0 smoke.png)"
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${RAG_OCR_SMOKE_TOKEN}" \
  -H "Content-Type: application/json" \
  --data "{\"media_sha256\":\"${media_sha256}\",\
\"ocr_revision\":\"paddleocr-3.5.0-ppocrv5-server-det-rec-paddle-static\",\
\"media_type\":\"image/png\",\"content_base64\":\"${content_base64}\"}" \
  http://127.0.0.1:18090/v1/ocr
```

真实请求必须带原始媒体 SHA256、固定 `ocr_revision` 和 Bearer token。完成后
只停止这个明确命名的临时容器：

```bash
docker stop rag-ocr-curl
```

本地已做的 CPU 冒烟只证明代码/模型接缝：一张真实 DOCX 内 PNG 得到 51 行、
189 个非空白字符、均值置信度 0.943705，加载 1.632 秒、推理 12.854 秒。
生产验收必须使用上面的 GPU 路径重新测量，不能引用 CPU 数字代替。

## 4. 保存与组成离线包

先按项目说明构建应用镜像并准备固定 Qdrant 镜像，然后执行：

```bash
RAG_APP_IMAGE=docx-rag:0.1.0 \
RAG_OCR_IMAGE=docx-rag-ocr:0.1.0 \
RAG_QDRANT_IMAGE=qdrant/qdrant:v1.18.3 \
bash deployment/package.sh
sha256sum artifacts/rag-docx-offline-*.tar
```

打包脚本保存三张 linux/amd64 镜像，并加入私有 DOCX、人工冻结集、评测运行时、
OCR 来源/依赖 manifest、CycloneDX SBOM、PaddleOCR/NVIDIA 许可证和总
`MANIFEST.sha256`。源代码 Git 不跟踪这些产物。

## 5. 服务器离线加载

目标服务器只接收已经校验的 tar，不 build、不 pull、不安装、不下载：

```bash
sha256sum rag-docx-offline-0.1.0-linux-amd64-*.tar
tar -xf rag-docx-offline-0.1.0-linux-amd64-*.tar
cd rag-docx-offline-0.1.0-linux-amd64-*
cp .env.example .env
# 人工填写四个不同令牌、模型端点、DOCX 路径和宿主 GPU ID。
bash verify-offline.sh
bash deploy.sh ./.env
docker compose --env-file .env -f compose.yaml ps
curl -fsS http://127.0.0.1:8088/live
docker exec rag-ocr python -c \
  "import paddle; print(paddle.__version__, paddle.device.cuda.device_count())"
```

服务器应同时监控容器重启、显存、单次 OCR 延迟和 126 个唯一媒体状态。
PNG/JPEG 应进入成功、低置信或明确失败状态；低置信文本不能单独支撑确定回答。
EMF 只有在另行冻结转换器二进制、SHA256 和许可证后才可启用；当前会明确
返回 `EMF_RASTERIZER_UNAVAILABLE`。

## 6. 回滚

```bash
bash rollback.sh ./.env
docker compose --env-file .env -f compose.yaml ps
```

回滚切换应用、OCR、Qdrant 的上一镜像 ID，不删除 `rag-state` 或
`rag-qdrant-data`。索引内容仍按 manifest 指向的 Qdrant snapshot 恢复。

## 官方参考

- PaddleOCR 3.x OCR pipeline：
  https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md
- PaddleOCR 3.5.0 release：
  https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.5.0
- PaddlePaddle Docker 安装说明：
  https://www.paddlepaddle.org.cn/documentation/docs/en/install/docker/linux-docker_en.html
- 固定基础镜像详情：
  https://hub.docker.com/layers/paddlepaddle/paddle/3.3.0-gpu-cuda12.6-cudnn9.5/images/sha256-bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776
