# 源码发布清单

- [ ] `git status --short --ignored` 中源码可见，私有/大型资产均为 ignored。
- [ ] `git ls-files` 不含 DOCX、冻结集、模型、tokenizer、wheel、镜像或 SBOM。
- [ ] 所有示例仅使用环境变量、占位符或 `.example.invalid`。
- [ ] `scripts/check_release_safety.py` 对 Git index 的所有分类计数为 0。
- [ ] compileall、Ruff、扩展 mypy、pytest 全绿，pytest skipped=0 且不少于
  基线 99 项。
- [ ] 四个 `deployment/*.sh` 通过 `bash -n`。
- [ ] `.env.example` 通过 Compose config，只有 `rag-app` 发布端口。
- [ ] `evaluation/frozen/MANIFEST.sha256` 校验通过，冻结题和阈值无改动。
- [ ] OCR 模型、wheel manifest、许可证、基础镜像 digest 和 pipeline 配置齐全。
- [ ] `git diff --check` 与 `git diff --cached --check` 均通过。
- [ ] 本地提交按卫生、核心代码、测试、部署、文档分组；仓库无 remote，
  push 次数为 0。
- [ ] `BLOCKED.md` 区分本地已证实与必须由用户在 GPU/目标服务器执行的项目。
