# PaddleOCR-VL-1.6 自托管 Demo

这里保留 PaddleOCR 3.7.0 官方 NVIDIA Compose 形状所需的最小仓库配置，运行的是
`PP-DocLayoutV3 + PaddleOCR-VL-1.6-0.9B` 完整文档解析产线，不是裸 VLM。
它独立于 RAG 主 Compose，默认只把 `8080` 绑定到宿主回环地址。
样例默认使用 `paddleocr3.7-nvidia-gpu-offline` 镜像标签，避免 `latest` 漂移；如需
更换镜像，必须同时核对 API 与 VLM 镜像的 PaddleOCR 版本。

先复制配置并按当次机器情况填写镜像标签和空闲 GPU：

```bash
cp deployment/paddleocr/.env.example deployment/paddleocr/.env
docker compose --env-file deployment/paddleocr/.env \
  -f deployment/paddleocr/compose.yaml config
docker compose --env-file deployment/paddleocr/.env \
  -f deployment/paddleocr/compose.yaml up -d --wait
```

RAG 控制台的“模型服务”中新建 `PaddleOCR` 连接，选择“本地/自托管完整产线”，
文档解析 Base URL 填写 `http://127.0.0.1:8080` 或实际可达的宿主地址。RAG 调用
`POST /layout-parsing`，并用原始 PDF 的真实物理页数核对返回页集合；配置中的
`Serving.extra.max_num_input_imgs: null` 只解除服务端默认截断，不能替代该核对。

关键负号、小数、单位、否定词、日期或版本号的二次核对需要另一个完整
PP-OCRv6 Pipeline 服务。在同一连接的“PP-OCRv6 复核 Base URL”填写该服务根地址，
RAG 才会对最终答案中的有界 bbox 调用 `POST /ocr`。留空时不会调用，也不会把状态
伪装成已验证。官方 API 模式则由 PaddleOCR 3.7.0 Python SDK 使用同一 Access Token
分别调用 `PaddleOCR-VL-1.6` 和 `PP-OCRv6`。

本目录不包含模型权重、Access Token 或私有 PDF，也不会自动修改或启动现有生产主机。
