# 源码发布与 PaddleOCR 交付验收

日期：2026-07-27
范围：本地源码、P0 评测逻辑、OCR CPU 接缝与离线部署资料
结论：本地范围通过；GPU 镜像与目标服务器范围待用户执行

## 本地已证实

- 发布候选 157 个文本文件；私有路径、RFC1918、本机路径、
  疑似凭据、二进制和超过 1 MB 文件均为 0。
- compileall、Ruff、扩展 mypy、115 项 pytest、四个 shell 脚本、
  Compose config、冻结 manifest 和 diff check 全绿；pytest skipped=0。
- evaluator 不再信任结果自报 chunk 合法性，伪造 ID 会使质量门禁失败；
  负载统计中的全拒答、错误拒答、意外回答和无效引用不能假通过。
- PaddleOCR 3.5.0 的 det/rec 模型与许可证按固定 URL、字节数和 SHA256
  校验；CPython 3.10 的 59 个 wheels 按独立 manifest 全部校验。
- 真实 DOCX 内一张 PNG 的 CPU 冒烟成功：51 行、189 个非空白字符、
  均值置信度 0.943705，未记录识别原文。
- Compose 只向宿主发布应用端口；OCR 单 GPU、非 root、只读、内部网络、
  单并发，Qdrant 不发布宿主端口。
- 仓库无 remote，执行 push 次数为 0。

## 用户待执行

- 按手册完成固定基础镜像准备和 `--network none` OCR build；本任务明确没有
  执行 `docker build/save`。
- 在 GPU 上完成断网模型加载、health/ready、真实图片请求、显存与耗时记录。
- 生成三镜像 SBOM 和单一离线包，在目标服务器执行 checksum、load、
  `up -d --no-build --pull never`、健康检查与回滚。
- 冻结 EMF 转换器二进制/许可证前，18 个 EMF 引用应保持明确失败状态。
- 取得生产模型 revision 和人工冻结参数后，再执行真实 6 文档入库、质量和
  并发性能验收；当前 provisional 参数不能 ready。

详细阻塞和解除条件见根目录 `BLOCKED.md`。
