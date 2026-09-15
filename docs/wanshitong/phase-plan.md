# 湾事通阶段计划

## 共同规则

- 每个阶段从最新 `origin/feature/wanshitong` 创建独立 `codex/wb-*` 分支。
- 每阶段先运行 `scripts/check_wanshitong_universal_first.py`，再运行该阶段定向门禁。
- 阶段通过独立验证后才允许合入集成分支；不得自动修改
  `feature/universal-rag` 或 Industry。
- Universal 已有 Product Runtime、问答、引用、流式、History 与 Trace 始终是当前
  能力，不列入湾事通待重写事项。

## WB-00：固定基线与不可回退合同

只新增五份设计文档和 Universal Preservation Gate，不改变运行行为。

验收：固定 SHA/祖先正确，受保护 Core 与基线测试存在，无第二套同名 Runtime，
`doctor` 与 diff 门禁通过，阶段分支已推送但未合入。

## WB-01：固定 Scope 组合

候选范围：在 `src/rag_app/wanshitong/` 增加薄组合层和服务端配置，将固定 Project/KB
及文档分类元数据转换为 Universal 已有请求和过滤合同。

不得修改检索算法、索引身份、Provider 数据面或 Qdrant schema。若需要数据库变化，
只能采用向前兼容 migration，并单独验证升级与回滚边界。

## WB-02：公共问答 Facade

候选范围：增加独立 route registrar，把无登录公共请求绑定到固定 Scope，并调用既有
`RagSdk` / `P09AnswerStream`、History、Trace、缓存和 Singleflight。

不得复制 `p09.py`、`p09_stream.py`、QueryService、Grounded Runtime 或流式协调器。
长期 Token 不进入浏览器。

## WB-03：公共与管理员界面收敛

候选范围：实现公共 `/` 与管理员 `/admin` 壳层，复用现有 API 和状态；公共界面隐藏
Project/KB，第一版不展示快捷入口。未配置 PaddleOCR-VL 时明确禁用 PDF 上传。

不得复制 Industry 静态前端或重建管理后端。

## WB-04：内网部署 overlay 与 Demo smoke

候选范围：新增独立 deployment overlay，把单个 Universal Connection 指向已验证的
Industry 模型 Endpoint，并以 HTTP `8188` 提供服务。

只运行阶段所需 build、入口/API smoke 与 Provider/OCR 配置检查；不因此宣称公网安全、
生产就绪、长期稳定性或模型质量达标。

上述后续阶段只是边界与依赖顺序，均需独立 Prompt 授权，不得在 WB-00 提前实现。
