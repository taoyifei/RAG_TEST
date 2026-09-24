# 上游实现与本方移植对应

固定上游：`Tencent/WeKnora@1edcd54b43606d9079bb36650efe3f68707a79ea`（v0.8.0，MIT）。升级时先比对这些路径与协议，再重建二进制、迁移索引身份并重跑接入验证。

| 能力 | 固定上游位置 | 本方位置 | 实际改动与边界 | 验证 |
|---|---|---|---|---|
| 常规检索和自然回答 | `internal/application/service/chat_pipeline/search.go`、`merge.go`、`into_chat_message.go`、`chat_completion_stream.go` | `application/retrieval/weknora_pipeline.py`、`application/answering/natural_answer.py` | 借鉴原问检索、重排后回读、编号材料和自然生成的流程；保留本方 Dense/Lexical/Exact、RRF、模型传输、Scope 与 Trace。当前未启用可选 query rewrite。仅核对引用绑定，不宣称逐 Claim 语义验证。候选正文在引用核对后分段输出。 | 候选链和管理员 SSE 定向 pytest；8289 结果见接入报告。 |
| 自适应父子分块 | `internal/infrastructure/chunker/*.go` | `vendor/weknora-chunker/internal/infrastructure/chunker/`、`cmd/wb-chunker`、`adapters/chunkers/weknora/` | 20 个上游 Go 算法/测试文件逐字节保留；本方只增加 JSON CLI、标准库 logger 与图片引用小桥接。Go 1.26.0 离线编译，固定二进制 SHA-256 为 `491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f`。父级仅回读，子级进入检索；新索引有独立指纹。 | `scripts/build_weknora_chunker.sh`、Go 原测试、本方父子 Revision 与来源映射 pytest；8289 结果见接入报告。 |
| 多格式解析协议 | `docreader/proto/docreader.proto`、`docreader/parser/` | `adapters/parsers/weknora_proto/`、`weknora_docreader.py`、`document_router.py` | Proto 原文 SHA-256 为 `b6ca2def2757610620b5d451e0ffa282950ef7c5dff5ee23bcc127c040f2c6a7`；生成绑定仅调整包导入与类型注解。DOCX 仍走本方原解析器，MD/TXT 本地读取，PPTX/XLSX/CSV 显式接入 docreader gRPC。远端格式须通过服务引擎探测和逐格式实测后启用；解析产物引用标为 `parsed_artifact`。 | gRPC 协议、格式路由、Markdown Job 和前端定向测试；8289 格式能力见接入报告。 |

上游 MIT 原文随 `vendor/weknora-chunker/LICENSE` 保存。Go 桥接、Python 适配与集成测试是本方代码。

docreader 固定为 Linux amd64 镜像 `wechatopenai/weknora-docreader@sha256:546aa7902310144e851a6e8dfd5a3e713aca4e7ca4456782a1efc69e7359708b`。镜像标签中的上游提交为 `1edcd54b43606d9079bb36650efe3f68707a79ea`，服务在容器内监听 gRPC 50051。8289 隔离验证时，docreader 与应用放在同一私有 Docker 网络，不发布 docreader 主机端口；应用显式设置 `RAG_WK_DOCUMENT_FORMATS=md,txt,pptx,xlsx,csv`、`RAG_WK_DOCREADER_ENDPOINT=<私有网络内服务名>:50051` 及 `RAG_WK_CHUNKER_MODE=parent-child`。未配置这些候选开关的部署保持原格式与分块行为。PPTX、XLSX、CSV 的真实结果见接入报告；升级上游时应重新固定镜像摘要并逐格式验证。
