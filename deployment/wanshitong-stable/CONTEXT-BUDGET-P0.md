# 原生知识问答上下文预算 P0

记录日期：2026-09-26。范围为 `wanshitong-stable` 的 8289 候选，固定源码基线 `2162f5f62ecadda8b61974ec61641637fb158460`。本记录依据用户现场截图、仓库配置和固定版 WeKnora 源码；未取得该次请求的最终模型输入、原生 Trace 或候选模型服务的分词结果，也未修改服务器。

## 复现与影响

用户问“固定资产从什么时候开始折旧？”时，模型接口返回 HTTP 400：物理上下文上限为 8192 token，实际输入至少 6145 token，请求输出上限为 2048 token，合计至少 8193 token。前台显示“处理未完成”和原始上游错误，引用区仍有 3 段。这说明检索已返回候选段，但模型没有接受本次生成请求；不能据此判断那 3 段是否足以回答问题。

此前验收记录中，“研发外协费用需要先有预算吗？”也曾触发同类 8192 上下文错误。将候选的输出上限设为 2048、`rerank_top_k` 设为 3 后，该题完成，但新的固定资产问题再次越界。因此该配置只能算特定样本的缓解，不能证明长上下文可靠。

## 固定版源码证据

- `deployment/wanshitong-stable/config/config.candidate.yaml`：`conversation.max_rounds: 5`、`rerank_top_k: 3`、`conversation.summary.max_completion_tokens: 2048`。
- `deployment/wanshitong-stable/config/builtin_models.candidate.yaml`：候选 `Qwen/Qwen3-8B-AWQ` 声明 `context_window: 7936`、`max_output_tokens: 2048`。截图中的 8192 是模型服务报告的物理上限；当前缺少这次请求的数据库模型行与最终送模报文，不能把 YAML 声明当作已核实的运行值。
- `vendor/weknora/internal/application/service/session_knowledge_qa.go:79-86` 从会话配置构造 `SummaryConfig`；`chat_pipeline/common.go:37-63` 将其中的输出上限交给模型；`chat_pipeline/references.go:16-107` 在检索内容和历史组装后形成最终模型消息；`chat_pipeline/chat_completion_stream.go:73-109` 随即调用 `ChatStream`。相同预算风险也存在于 `chat_pipeline/chat_completion.go` 的非流式调用。
- `ContextWindow` 在固定版普通知识问答送模路径中没有用于限制最终消息。Agent 路径的 `session_agent_qa.go:103-115` 会读取模型窗口，但该路径不能证明普通 RAG 受相同保护。
- `vendor/weknora/internal/agent/token/estimator.go` 使用 `cl100k_base` 估算，并明确说明非 OpenAI 模型的 token 数不精确；代码注释指出 Qwen 中文场景的比例可能接近 0.6，Agent 可在取得真实 Usage 后校准。普通 RAG 没有对应的精确计数或校准通路。把该估算直接作为拒绝、缩短输出或裁剪证据的硬门禁，可能错拦原本能回答的问题，也无法保证消除临界 400。

## 最小可行修复条件

1. 在 8289 使用的实际模型服务上确认物理窗口、部署版本、模型 ID，以及**与聊天生成相同 chat template 的精确请求 token 计数接口或分词器**。官方 vLLM 文档列有 `/tokenize`，但它是否能准确计入当前聊天模板、特殊 token 与消息封装，必须对该候选服务验证，不能仅凭端点名称假定等价。若模型服务不能提供等价计数，选择经验证的更大窗口模型是另一条可行路径。
2. 在 WeKnora 原生层对最终编码后的系统消息、历史、检索证据和用户消息做预算检查，统一覆盖流式与非流式知识问答。预算使用实际模型窗口、当前输出上限和安全余量；记录模型 ID、输入 token、输出预留、窗口与请求 ID，不记录明文提示词或密钥。
3. 预算不足时给出受控、可解释的容量错误。不得在湾事通外壳按关键词裁剪，不得静默删除证据、修改引用或把不完整回答标成完成。调整历史/检索内容的策略须在原生层另行设计并显式验收；仅把 2048 改为 2047 不能解决其他长度的请求。
4. 回归覆盖本次 6145+2048 边界、低于/等于/高于窗口的输入、五轮历史、长检索段、单段已超限、流式和非流式、引用一致性及真实模型 token 结果。8289 重问截图问题，核对实际送模计数与最终回答；对仍超限的请求应在模型调用前得到受控终态。

当前状态：**P0，原生预算修复阻塞**。本轮未加入基于近似分词的硬裁剪，也未修改模型配置、候选服务或公共问答语义。上游 400 在湾事通页面的安全分类属于接入层错误处理，需另行核验；其完成不等于原生上下文预算问题关闭。
