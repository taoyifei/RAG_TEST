# 湾事通轻量门禁

## 适用范围

本门禁只用于 WB-00 分支与文档交付。它不能替代后续 Runtime、API、数据库、前端、
Docker、模型或 OCR 阶段的定向测试。

## 必过门禁

### 1. 基线门禁

- `git fetch --prune origin` 成功；
- `origin/feature/universal-rag` 必须精确等于
  `4f71f7db03990cf49cde0664bec68d6c25dfd544`；
- 若不相等，立即输出 `BASE_DRIFT`、旧 SHA 和新 SHA，并停止代码或文档修改；
- `Industry` 只读参考固定为
  `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。

### 2. 分支门禁

- `origin/feature/wanshitong` 从固定 Universal SHA 创建并已推送；
- `codex/wb-00-bootstrap` 从最新 `origin/feature/wanshitong` 创建；
- 阶段分支与集成分支的 merge-base 等于固定 Universal SHA；
- 不 merge、rebase 或 cherry-pick `Industry`，不自动合入 `feature/wanshitong`。

### 3. 范围门禁

相对起始 SHA 的改动只允许以下文件：

- `docs/wanshitong/architecture.md`
- `docs/wanshitong/demo-boundaries.md`
- `docs/wanshitong/phase-plan.md`
- `docs/wanshitong/ocr-capability.md`
- `docs/wanshitong/lightweight-gates.md`
- `docs/progress/wanshitong.md`

`docs/` 受仓库忽略规则影响，暂存时只能显式选择上述文件，不得用宽泛路径带入其他
文档或现有未跟踪内容。

### 4. 文档人工核对

逐项确认以下合同出现且没有被描述为 WB-00 已实现：

- 公共 `/` 与管理员 `/admin`；
- 固定隐藏 Project/KB、一个知识库与文档元数据过滤；
- 无登录公共会话，Token 不进浏览器；
- 7 天加密 History；
- 第一版不展示快捷入口；
- Industry 模型 Endpoint、最高 4 并发和 `4 active + 8 queue`；
- 内网 HTTP `8188`；
- Industry PP-OCRv5 `POST /v1/ocr` 与 PaddleOCR-VL
  `POST /layout-parsing` 明确区分；
- 当前不部署新 OCR，未配置 PaddleOCR-VL 时明确禁用 PDF 上传；
- WB-00 只做轻量门禁，不声称运行时或生产验收完成。

### 5. 命令门禁

必须运行：

```text
git diff --check
```

提交前还应对暂存差异运行 `git diff --cached --check` 并核对文件清单。可选运行：

```text
.venv/bin/python scripts/dev.py doctor
```

## 明确不运行

WB-00 不运行全量 pytest、全量 mypy、全部 Playwright、完整 release acceptance、长时
压测、fuzz、SAST 或渗透测试。未运行项必须在交付中如实列为未验证边界，不能用文档
检查的 PASS 替代运行时结论。
