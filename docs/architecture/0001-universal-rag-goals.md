# ADR 0001：通用 RAG 目标与边界

- 状态：Accepted
- 日期：2026-09-01

## 背景

当前仓库已实现安全 DOCX 解析、稳定来源定位、混合召回、RRF、重排、严格引用、
Trace 和恢复能力，但运行时仍把 Web、Qdrant、模型 HTTP schema 与 DOCX 路径一起组装。
Industry 分支还包含特定语料、镜像、服务器和恢复流程，不能成为新的通用主线。

## 决策

1. 产品目标是可嵌入其他项目的通用 RAG 组件，而不是行业部署包。
2. 首期只把 DOCX 做深做稳；内部 Document IR 不得暴露 OOXML 类型或假设只有 DOCX，
   以便后续接入 PDF、HTML、Markdown 等 Parser adapter。
3. 通用核心不得依赖 FastAPI、Qdrant Client、具体厂商 HTTP schema或 DOCX OOXML。
4. 保留现有安全解析、来源定位、混合召回、RRF、重排、证据引用和可观测性；通过
   adapter 逐步解耦，不推倒重写。
5. 继续使用 Python 包 `rag_app`。领域模型和端口后续进入 `rag_app.core`，用例编排
   进入 `rag_app.application`；不得另建平行顶级包 `universal_rag`。
6. `main` 和 `Industry` 在本轮都不是阶段合并目标。阶段只合入
   `feature/universal-rag`。
7. HyDE、Query2doc、RAPTOR、Late Chunking、LLM Contextual Retrieval、GraphRAG
   和迭代式 Agent Retrieval 仅能作为关闭的实验开关；没有冻结数据集、真实 Provider
   和可复现实验，不得改变默认值或宣称效果提升。
8. `feature/universal-rag` 不自动合入 `main`；如需合入必须单独触发决策门。

## 本阶段不代表

- 尚未实现完整通用组件、Document IR、新 Provider、新 Parser 或新前端。
- 尚未证明生产性能、检索质量提升或任一真实模型/服务器可用。
- 尚未修改现有公共 HTTP API、配置键或持久化数据模型。
