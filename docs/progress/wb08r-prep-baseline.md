# WB-08R 准备阶段基线

冻结时间：2026-09-21T07:49:06Z

## 四类身份

| 身份 | 冻结值 |
| --- | --- |
| 准备阶段起点 | `c2934e0031e47fa97bcd168c34630207b4982234` |
| 代码/回滚候选 | `6187be6f0bc9d822101ab5268b0b7eebd1905970` |
| 8289 应用镜像 | `rag-test-wanshitong:wb08r03g-6187be6` / `sha256:8da303c05f71e770be10f8845c0d336ad0feeb72839eb5a1baa6ae9de27cb9c1` |
| 产品资产 | manifest `8c1f323bced7ef489962283b4a61cbaaabfe07ad835bd980751421d77cf55b3e4`，37 个文件，1,107,812 bytes，source revision `6187be6` |
| 活动索引 | `irev_0329a35ca9ea700133f7b309160118f0`，46 个活动文档，向量维度 1024 |
| 题库 | `wanshitong-v2-20260917`；具体文件哈希见 `evaluation/wanshitong/prep-v1/manifest.json` |
| 外部依赖 | embedding `Qwen3-Embedding-0.6B`；reranker `Qwen3-Reranker-0.6B`；LLM `Qwen/Qwen3-8B-AWQ` |

## 运行与隔离边界

- `10.242.180.54:18288` 是生产入口转发，目标为 `10.242.180.60:8288`；
  本阶段禁止停止、重启、替换或改写该链路。
- 专用候选为 60 上的 `wanshitong-wb08r01-app`，仅绑定
  `127.0.0.1:8289`，当前健康，容器用户为 `rag:rag`（10001:10001），
  root filesystem 只读。
- 8289 的 `/data`、`/logs`、`/run/rag-secrets`、Qdrant 数据目录均与
  8288 生产实例分离；`/data` 与 `/logs` 的运行用户权限已核对。
- 真实测试只允许经 54 跳板访问 8289，不在 8288/18288 上执行写请求。
- 本阶段不会替换生产。只有用户在测试通过后另行授权，才可进入替换步骤。

## 当前 Trace 工作区证据

以下十个既有文件均为本地 Trace/评审结果，只记录身份，不提交内容。它们所在
目录已整体加入 `.gitignore`，文件本身未删除：

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `wb08r03-0ebc6b9-functional96.ndjson` | 48,373 | `be5dc2a54cafcefdac145c9623ac2359ea639140d6b890dfb8a81a9b94229fa9` |
| `wb08r03-0ebc6b9-private-review.ndjson` | 85,293 | `37cb6956d6a11d0382a50774769757a07a5f03ec48a8791d547e18ce01a522be` |
| `wb08r03-50ff77e-functional96.ndjson` | 55,980 | `a875d2a31de4e5919982af48e6c15b88f004bd031f1f7ac1820d41cc981729c7` |
| `wb08r03-50ff77e-private-review.ndjson` | 111,956 | `c5aa00111956fd432cddfc6c2f6242d89ae6a476e4215e8467cd66ebe55c7c18` |
| `wb08r03-50ff77e-trace-v2.json` | 68,404 | `1c9fb6fd88ed74d53be963888e6612a54494f9e7dce2b2cfd5b64252fc5b9b7f` |
| `wb08r03-50ff77e-trace.json` | 63,180 | `986afe09bac4c98796e9637680dd03914c6ded24cee2945c32d68639bd88d249` |
| `wb08r03r-2b55116-terminal12-private.ndjson` | 9,857 | `5ff2ea1800de469049b56dd088affb3b590053953903eb200a91fd8738dfc621` |
| `wb08r03r-2b55116-terminal12.ndjson` | 8,864 | `417e76c784acf195f581d7d24aa788ca56dab4a7a527aac821e7987e80efb315` |
| `wb08r03r-4390fab-terminal12-private.ndjson` | 11,096 | `7022ba8b8764575f828c82091c5ce42f25605ba866fe405c8090c2eb3d87542c` |
| `wb08r03r-4390fab-terminal12.ndjson` | 8,828 | `1e2af75cfdb5fb2c40f7362265f0c5c2ac64fc8836c9fd713fc5dcbe0a351bb2` |

## 准备阶段边界

- 只修复 F048 的输出 atom 范围与“生成成功、校验未完成”归因，不重新执行
  A-E 全量回归，不做无关优化。
- 先运行定向离线门禁，再在 8289 运行 Replay6 和 Smoke12；不运行 Full96。
- 新问题最多进行三轮有证据的修复。仍失败则登记 P0、清理本次过期 8289
  镜像并提交，不进入无限修复。
- SSO 仅登记外部准备项，不虚构地址、凭据、负责人确认或联调结果。
