# P00：基线、缺口与最小移植边界

## 已完成

- 以 WSL 原生 Git 核对本方远端与本地 `codex/wb08r-q1-algorithm-debt` 均为 `ac6547c6b2c0da895a181b0b731736837b9af1c8`，新建独立 `codex/wb08r-context-reader-v1` worktree；原分支和 `main` 未改动。
- 核对 WeKnora v0.8.0 固定提交、目标函数及 MIT 许可，记录主动差异，见 `UPSTREAM_PORTS.md`。
- 只读追踪对外 `54:18288 → 60:8288 → 60:18290` 与 `54:8289 → 60:18289 → 60:8289`，核对两端实际应用容器、活动 SQLite revision、数据目录与 Docker 网络。两者使用同名 Qdrant URL，但 DNS 分别解析到各自网络内的不同容器；本轮未切换服务。
- 从公共接口追踪到实际模型 HTTP 发送，见 `source-mapping.md`；用受限 Trace、活动 8289 SQLite 和 DocumentIR 确认 L1-L4，见 `loss-ledger.json`。公开文件不含原文、凭据、Cookie 或完整私有 Trace。

## 已证实缺口

F017 的两块关键来源已进入重排输入，但重排结果的 12 个名额全部由受保护候选占据。后续表格精确回读在活动索引的 7 个目标行 Chunk 中只取回 2 个，另 5 个纵向合并的重复片段因 `source_anchor.row_index=NULL` 被 SQL 谓词过滤；旧来源身份校验也拒绝这些片段。权威 DocumentIR 与源跨度足以证明目标共享节点可完整恢复。旧发送包只含时限列的物理事实，生成后错误 Claim 被语义门拦截，最终回答不完整。

旧 P0 文本中的一个 Chunk ID 多写了一个字符；已由活动 SQLite 与原始 Trace 更正。因此“该块融合前缺席”的推断不成立。旧记录保留历史诊断语义，另作单字符修正。

## P01 最小实施范围

1. 为同一 frozen ranked seeds 增加 `legacy|shadow|candidate` 配置和指纹隔离，默认 `legacy`。
2. 以活动 revision 下的 DocumentIR、表格 atom 成员映射和精确源节点回读，恢复表头、目标逻辑行及共享物理单元格；按同一源坐标核对重叠、缺口、part/story 和版本。
3. 建立有界完整来源组与组级预算；候选生成材料直接从这些组形成，不再经旧成员 Top-K 裁散。`shadow` 不新增模型调用。
4. 复用 `EvidenceReadUnit`、`PhysicalTableFact`、`PreparedGenerationPacket` 与现有来源/Claim 核验。终端组预算不足要按组回退或明确失败，不静默裁剪片段。
5. 用合成结构测试及 8289 真实功能回放验证接入；阻塞题未全过时记录 P0 和未解决层次，不切 18288。

## 未完成与准出

P00 没有修改生产算法、重建索引、部署候选、调用真实模型或宣称质量改善。三个模型的实际运行端点与版本身份仍待候选部署时从安全配置核对；不得把源码默认值当成现网真相。当前证据已明确缺失位置、权威关系来源及接入边界，具备开始 P01 的条件。
