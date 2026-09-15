# 湾事通 Universal-first 不可回退合同

## 固定基线

- Universal Product Runtime：
  `feature/universal-rag@4f71f7db03990cf49cde0664bec68d6c25dfd544`。
- 湾事通集成分支：`feature/wanshitong`，以该 SHA 为祖先。
- Industry 只读参考：
  `Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。

湾事通是在 Universal 当前 Product Runtime 上增加薄壳，不是将 Industry 搬回
Universal，也不是重写第二套 RAG。

## 唯一职责关系

| 层 | 唯一职责 |
|---|---|
| Universal RAG | 文档解析、Document IR、Chunk、Index Revision、Qdrant、Exact/FTS/Dense、RRF、Reranker、Evidence、Grounded LLM、引用校验、会话、History、Operational Trace、反馈、缓存、Singleflight、备份与恢复 |
| 湾事通新增层 | 固定隐藏 Scope、无登录公共问答 Facade、新公共前端、管理员页面收敛、内网部署 overlay、文档分类元数据和未来快捷入口扩展点 |
| Industry | 内网模型名称、Endpoint 协议、端口、环境变量示例、OCR 现状和部署经验的只读参考 |

Universal 已有能力是湾事通的当前基础，不能写成“后续再做”。湾事通层只能组合、
收敛或限制既有能力，不能接管其数据面。

## 必须保留的 Universal 主干

以下类型与能力继续作为唯一实现：

- `ProductRuntime` 与 `ProviderRuntimeRegistry`；
- `openai-compatible` adapters 与 `RetrievalProfile`；
- `RagSdk` 与 `P09AnswerStream`；
- `ProductQueryHistory` 与 `ProductTraceCoordinator`；
- Grounded Answer、Claim Validation；
- SourceSpan、Evidence、Citation；
- 既有检索、索引身份、缓存、Singleflight、备份与恢复链路。

受保护文件清单与基线展开结果由
`scripts/check_wanshitong_universal_first.py` 机械核对。原则上只读；后续阶段确需改动时，
必须逐文件解释原因、证明改动最小并运行对应回归，不能以新壳层为由改写 Core。

## Provider 与 Endpoint 合同

- 内网与公网 Provider 对应用层的主要差异是 URL、网络可达性和凭据，不是检索或生成
  算法。
- 湾事通首先使用 Universal 已支持的单个 `api_base_url` / Connection，并复用现有
  `ProviderRuntimeRegistry` 和 `openai-compatible` adapters。
- Industry 中的 Endpoint 数组只提供部署参考。若现有 Universal 抽象不能直接表达多
  Endpoint，则明确选择主 Endpoint；不得新增 Fleet Manager、第二套 Provider Runtime
  或 Industry 专用客户端。
- 凭据继续由 Universal Credential/Connection 边界管理，不进入浏览器或湾事通静态
  配置。

## 禁止行为

- merge、rebase、subtree merge 或批量 cherry-pick Industry；
- 复制 Industry 的 QueryService、FastAPI App、检索/生成/流式链路、状态库或静态前端
  覆盖 Universal；
- 新增第二套 Provider Runtime、RAG Pipeline、History、Trace 或 Qdrant 索引身份；
- 删除、降级、绕过或把上述 Universal 能力改列为未来工作；
- 删除、skip、xfail、弱化既有问答回归，或用 Mock-only 结果替代真实目标合同；
- 为本阶段之外的内容格式化或重构 Core。

## 每阶段证明义务

提交前必须审阅相对阶段起点的 `git diff --name-status`、`git diff --stat` 和
`git diff --check`。自动门禁验证可机械判断的祖先关系、文件存在性、同名重复
Runtime、固定基线全部 `test_*.py` 与测试绕过标记；文件覆盖、语义弱化和不相关重构
仍必须通过完整 diff 人工判断。

任一红线命中时阶段状态为 FAIL，停止提交或推送成功结论。
