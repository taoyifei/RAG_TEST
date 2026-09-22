# WB08R-03G V14.1 C5：协议接入收敛与 D3 实施报告

## 最终状态

**FAILED / PAUSED**。V14.1 C5 计划的代码工作包已在真实测试前逐项核对，相关离线门禁
通过；受控 A/B 随后确认旧 400 的精确原因并验证唯一性约束安全移交，但当前真实服务的
D3 协议资格只通过 1/5。根据用户“严格按计划后出现新问题不自行修改”的指令，本轮已
停止，不进入业务五题、候选部署、Preflight-16 或 A～F，也不具备生产替换资格。

## 实施范围

起点为 `de251f0324b82d8c406a81a616f405629e839c96`，严格实现后的协议测试源码为
`4c981fa4c8579c98c2881bee1106b57f749d19e6`：

- `d074bde6790448dae9a91113371e6f3fffcbaa56`：有界安全/私有 HTTP 失败诊断。
- `fd8cbe056aa6852b5d061a09e689848318f35a1d`：统一字段合同、能力 profile、执行状态、
  cache/终态隔离、D1/D2/D3。
- `4c981fa4c8579c98c2881bee1106b57f749d19e6`：按部署 tokenizer 固定字段输出预算、运行身份
  与资格定义。

没有调整召回权重、业务同义词、表格事实、BEFORE/MUST 规则、模型或 retry/repair。

计划 SHA-256：
`645e206a66e107f6cb5cd6cd82eed50f1d98c9964a92d8bc75818d7549df0c9f`。

## 真实测试前的一致性结论

逐项映射见 `docs/progress/wb08r-03g-v14-1-c5-code-alignment.md`。真实访问前已确认：

- 单一 `wb08r-field-resolution-v2` 声明同时生成发送 schema 和本地验证。
- Atom、候选、状态组合、q 锚点、重复 JSON key 及 EXACT 权限均有负例。
- 真实 Adapter + MockTransport 覆盖最终 HTTP body、固定 mode、400 诊断和单次请求。
- 字段执行状态与证据语义状态正交，系统失败不进入语义负缓存。
- 当前服务/模型/tokenizer/template/backend 身份及 8289/生产身份已安全固定。
- 部署 tokenizer 的合法输出测量为 193、58、959 tokens；字段输出预算固定为 1280，且
  `6144 + 1280 < 8192`。

相关验证：

- 问答/协议/错误/缓存/配置相关：`565 passed, 1 deselected, 1 warning`。
- 最终预算和现场资格定义针对性回归：`55 passed, 1 warning`。
- Ruff check / format：29 个改动 Python 文件通过。
- mypy：407 个 source files 通过。
- Compose 占位配置、导入和 `git diff --check` 通过。
- 未运行与问答功能无关的测试。

## 运行身份

- 推理服务：vLLM `0.19.1`，`Qwen/Qwen3-8B-AWQ`，AWQ，tensor parallel 2，
  `max_model_len=8192`。
- mode：`response_format`；部署命令无显式 structured backend 参数，配置身份记为
  `vllm-0.19.1:auto`，实际能力由本次 A/B 与 D3 验证。
- 服务 image ID：
  `sha256:b2527a6b9cfbd8c73c4a42dccf425173eac65554b05b60e7c357ab4838c922df`。
- 安全身份文件 SHA-256：
  `67bf43e71d4ea2ed6738194ce2cff5baf1645a6747cc92d352c186d8afeeeed6`。
- D3 资格定义 SHA-256：
  `54da768d67c9587aadecba994f31fb5aecfeabca67fcaf1fb39d5821c2a7b119`。

## 受控 A/B 结果

`reconstructed-original` 发出一次旧 V1 C5 重建请求，收到 HTTP 400，完整私有错误正文为：

```text
Grammar error: Unimplemented keys: ["uniqueItems"]
```

`reconstructed-transferred` 仅从最终 HTTP payload 删除
`$.response_format.json_schema.schema.properties.r.items.properties.c.uniqueItems`，其它消息、
模型、mode、schema 字段及预算不变；一次调用得到 HTTP 200、`finish_reason=stop`。

结论是本连接只能将该唯一性约束移交严格本地验证。A/B 共 2 次真实 chat 请求，无 retry。

## D3 结果

| Case | 结果 | 证据 |
|---|---|---|
| `AMBIGUOUS` | PASS | HTTP 成功；两竞争字段身份、状态、q 锚点和 token usage 均符合真值 |
| `PARAPHRASE` | FAIL | 8011 ms `ReadTimeout`，没有 retry；服务端根因未观察 |
| `NOT_FOUND` | FAIL | HTTP 200 后被本地合同以 `FIELD_RESPONSE_STATUS_COMBINATION_INVALID` 拒绝；原始 content 未留存 |
| `TWO_ATOMS` | FAIL | 结构合法，但两个 Atom 均错误返回 `AMBIGUOUS` 和两个候选，而非各自一个等义字段 |
| `MAX_SHAPE` | FAIL | 7553-token 输入超过 6144 门禁，Provider 调用数 0 |

D3 共 5 case、4 次 chat 请求、0 retry、1 pass。A/B + D3 的 chat 总调用为 6；业务请求为
0。D3 safe/private SHA-256 分别为
`0fec7dd32a8b0684e5e990ec6f95e7f9f827e795746b0a760f0eecc153d04066` 和
`fcd7da7732e466b68430a19d8423c287f11c6b9744b0f13685a728bde205ac2c`。

详细因果、不可观察边界和恢复门槛见
`docs/progress/wb08r-03g-v14-1-c5-d3-p0-issues.md`。

## 门禁与发布

- D1：PASS。
- D2：PASS。
- D3：FAIL。
- 业务五题：NOT RUN。
- 五题变体、Preflight-16、A～F：NOT RUN。
- 候选镜像构建/8289 部署：NOT RUN。
- 生产发布：禁止；`merge_allowed=false`，`production_publish_allowed=false`。

## 运行安全与清理

- 8289 未被替换，仍运行 `rag-test-wanshitong:wb08r03f-bd00c56`，live/ready 均 200。
- 60:8288 与 54:18288 的生产容器/镜像身份未改，健康为 200。
- 本轮无候选镜像或构建 worktree 可清理。
- 已精确删除 60 上停止三天的历史 WB08R02 容器
  `wanshitong-wb08r02-app` 及镜像 `rag-test-wanshitong:wb08r02-9c3b029`；没有删除 volume，
  没有执行全局 prune。
- 原有 10 个未跟踪评测文件仍在，SHA-256 与开始时一致。

## 计划外历史问题

两项既有测试失败已在干净旧提交复现，未混作本次回归，也未修改：缓存首轮不可缓存导致
第二轮无法早命中，以及 certified excerpt 的 A2 source scope 期望 `SUPPORTED`、实际
`MISSING`。详见 P0 文档的独立章节。

## 证据位置

- 安全结果：`docs/progress/wb08r-03g-v14-1-c5-d3-evidence.safe.json`。
- 安全 Manifest：`docs/progress/wb08r-03g-v14-1-c5-d3-manifest.json`。
- 私有源证据：
  `/home/jerry/work/RAG-private-evidence/wb08r03-v141-c5-protocol-4c981fa`。
- Windows 可分析目录与 ZIP：在报告提交后组装，最终路径和 SHA-256 写回 Manifest。

私有证据含内部问题/请求正文，不上传 Git；安全报告不含密码、Token、`.env` 或私有正文。
