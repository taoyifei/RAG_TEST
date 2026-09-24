# 当前产品调用链与读取器边界

基线：`ac6547c6b2c0da895a181b0b731736837b9af1c8`。行号对应此基线，后续实现会使行号变化。

## 实际问答与发送链

`src/rag_app/wanshitong/public_api.py:199-279` 的 `POST /api/public/chat` → `src/rag_app/api/p09_stream.py:192-211` 的 `P09AnswerStream._run` → `src/rag_app/sdk.py:1067-1263` 的 `RagSdk.answer_stream` / `:983-1006` 的 `_execute_search` → `src/rag_app/application/retrieval/service.py:1011` 的 `RetrievalService.search_and_answer` → `:5017` 的 `_rank_and_select`（融合、hydration、重排、旧邻居扩展、旧 EvidenceAssembler）→ `:2334-2346` 的 `build_generation_evidence_pack` → `src/rag_app/application/answering/grounded.py:2855-2912` 的 `GroundedAnsweringService.answer` 与 `GenerationRequest` → `src/rag_app/adapters/providers/aliyun_chat.py:2256-2329` 的 `_prepare_generation` → `:2331-2356` 的 `generate` → `:2690-2712` 的实际 HTTP JSON 请求。带 `QueryPlan` 的 stream 调用转入同步 `generate`，公共 SSE 只包装响应流。

生产链使用 `application.retrieval`。根级 `retrieval/`、`generation/` 和 `query_service.py` 不是本次公共问答的替换点。

## 来源端口与已证实缺口

`EvidenceSourcePort` 定义在 `src/rag_app/core/ports/evidence_source.py:51-155`，实现在 `src/rag_app/adapters/stores/sqlite_control.py:1589-2072`。`hydrate_chunks` 按活动 revision 批量回读；`load_document_structure` 按成对文档版本分页；`section_chunk_ids` 的 limit 不证明完整；`table_context_chunk_ids` 超限返回 `None`。

8289 活动 SQLite 的 F017 目标表在 row 2 有 7 个 canonical Chunk，表头另有 1 个。相同 `table_node_id`、row 2 的 `cell_source_node_ids['1']` 指向一个 101 字的原始物理节点。现有 `table_context_chunk_ids` 对目标行只返回 2 个 Chunk，加表头共 3 个；另外 5 个只含 `is_repeated=true` 的来源 span，`source_anchor.row_index` 为 `NULL`。`sqlite_control.py:2036-2055` 要求 `atom.row_index > source_anchor.row_index`，因此 SQL 三值逻辑排除了它们。只改 SQL 仍不够：`neighbors.py:631-715` 的 `_verified_vertical_inheritance` 和 `_table_source_identities` 同样依赖非空锚点行号，后续身份校验会拒绝这些 Chunk。

活动 `revision_documents` 保存同文档版本的完整 DocumentIR（259 个节点、127 个 Chunk）；目标物理节点的 `text_payload.exact_text` 长 101 字。以真实 atom 映射、结构路径、part/story、文档版本和源字符范围，可以逐字验证 6 个 row 2 共享单元格片段连续覆盖 `[0, 101]`；不能凭文字相似、文件名或相邻行推断继承。

## 最小替换边界

`_rank_and_select` 完成原有召回、RRF、canonical hydration 与重排后冻结 ranked seeds。`legacy` 沿用原路径；`shadow` 对同一 seeds 运行新读取器但不送模、不开新模型调用；`candidate` 用新读取器按活动 DocumentIR 和精确 Chunk 源关系恢复来源组。候选生成包从新来源组构造，不再经过旧 `NeighborExpander → EvidenceAssembler` 的成员 Top-K 与 span 配额。旧查询规划和最终来源/Claim 核验保持不变。终端 `PreparedGenerationPacket` 的 read-unit 绑定、实际 body 摘要和移除原因是 L3 权威证据；若终端预算无法容纳完整组，只能按组回退或显式预算失败。

源码中现有 `EvidenceReadUnit`、`PhysicalTableFact`、`PreparedGenerationPacket` 及生成包结构继续作为阅读、表格关系与发送合同，不另建并行引用体系。
