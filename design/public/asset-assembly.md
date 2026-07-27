# 源码与离线资产装配

源码仓库不承载私有语料或大型二进制。以下目录必须保持 ignored：

- `docs/`、`evaluation/frozen/`、`evaluation/results/`、`artifacts/`
- `deployment/wheelhouse/`、`deployment/assets/tokenizers/`
- `deployment/ocr/assets/`、镜像 tar、SBOM、数据库和日志

## 应用资产

应用镜像需要 Python 3.11 的 `deployment/wheelhouse/`、两个本地 tokenizer
以及 `deployment/ASSETS.sha256`。装配者应从经批准的内部来源复制 tokenizer，
用 `requirements.runtime.lock` 下载 linux/amd64 wheels，构建项目 wheel，
再从仓库根目录执行：

```bash
(cd deployment && sha256sum --check ASSETS.sha256)
docker buildx build --network none --platform linux/amd64 \
  --load --tag docx-rag:0.1.0 .
docker run --rm --network none docx-rag:0.1.0 asset-selfcheck
```

如果 tokenizer 或配置有意变更，先更新 pipeline/资产版本，再人工复核并更新
对应摘要；不得只为通过校验而改摘要。

## OCR 资产

```bash
.venv/bin/python scripts/download_ocr_assets.py
.venv/bin/python scripts/download_ocr_wheels.py
(cd deployment/ocr/assets && sha256sum --check MANIFEST.sha256)
(cd deployment/ocr/assets/wheelhouse && \
  sha256sum --check ../../WHEELS.sha256)
```

`ASSET_SOURCES.json` 固定模型和 PaddleOCR 3.5.0 许可证的官方 URL、字节数及
SHA256；`requirements.lock` 和 `WHEELS.sha256` 固定候选基础镜像
Python 3.10 所需的 59 个 wheels。下载脚本拒绝路径越界、链接、特殊 tar
成员、摘要漂移和额外 wheel。具体镜像命令见 PaddleOCR 部署手册。

## 发布边界

装配完成后只打包镜像、私有输入、评测运行时、checksum、SBOM、许可证和
来源记录。不要 `git add -f` ignored 资产。发布候选须先暂存，再运行：

```bash
.venv/bin/python scripts/check_release_safety.py
git diff --cached --check
```
