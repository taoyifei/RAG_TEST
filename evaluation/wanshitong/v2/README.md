# 湾事通 V2 冻结评测集

此目录只供离线评测和阶段验收。题目、Seed、来源身份、行为和抽象事实原子
不得复制到 `src/`、`frontend/src/`、运行时 Prompt、索引或生产镜像。
本目录不保存标准答案正文，也不按题目生成生产规则。

## 来源与身份

上一版 `WB08-00` 仅规定 Formal-54 的构成，工作区和远端未发现已生成的
`v1/diagnostic-54.ndjson`。本版依据旧版 `WB08-00_Goal_冻结评测集与诊断合同.txt`
和 `01_REFERENCE_19_CASE_DECISIONS.txt`，从本地 WB07R 149 题与五轮
真实记录**按旧合同重建** Formal-54：17 条必答、2 条有限回答、14 条
未入库模板正文安全拒答、1 条时效菜单安全拒答、20 条同问题文本且五轮
均有引用回答的回归哨兵。旧 ID 曾发生换题，Formal 全部使用新的
`WB08R-F-*` ID，`seed_case_id` 保存旧 ID；Manifest 固定
`case_id → question_sha256`、文件 SHA-256 和旧来源 SHA-256。五轮“有引用”
只用于选稳定哨兵，不自动证明内容正确。

这 20 条回归哨兵的原问题在本阶段开始前已出现在
`frontend/src/public/suggestedQuestions.ts`。它们只用于已见问法回归，
不能作为未见自然问法的泛化证据；本阶段没有把 V2 题集导入或复制到前端。

Natural-60 依 `03_REFERENCE_真实用户评测设计.md` 独立写作并逐条对照 Seed
意图和来源检查，问法类别为 18 条短句/省略、15 条口语、9 条错字/缩写、
9 条多轮、9 条复合；56 条完整问题不超过 18 字。四类角色均覆盖，
多轮题含前一轮用户问题，不保存助手答案。Adversarial-30 的五类各 6 条。
Latency-24 是 Formal/Natural 原题的固定子集：Catalog/Navigation、
清晰单事实、复合/多轮各 8 条，新增的只有 `latency_bucket` 标签。

`required_atoms` 仅使用 `schema.json` 枚举的抽象结构标签，例如 `actor`、
`condition`、`time_limit`、`negation`；标签不含具体人名、时限数值或
答案片段。未来如果改变问题文本或多轮上下文，必须分配新 `case_id`，
不得改写旧题并沿用身份。

## 核验与结果合同

在仓库根目录用项目 Python 3.11 环境执行：

```bash
.venv/bin/python -m pytest -q \
  tests/evaluation/test_wanshitong_v2_identity.py \
  tests/evaluation/test_wanshitong_v2_distribution.py \
  tests/evaluation/test_wanshitong_eval_isolation.py
```

`validation.py` 检查题目摘要、Manifest 身份锁、四份文件的完整摘要与分布。
`schema.json` 同时定义未来结果的统一指标合同；无法由旧 Trace 确定的字段
写 `null`。`baseline-trace-analysis.json` 与 `baseline-latency.md` 冻结本轮
能够核实的真实延迟，以及本地 WB07R 缓存的证据边界。尚未运行新题集，
这里没有回答率或质量提升结论。
