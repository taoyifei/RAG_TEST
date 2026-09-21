# WB-08R 准备阶段冻结样本

`manifest.json` 在任何准备阶段真实回放前冻结 Demo20、Smoke12 和定向
Replay6。样本只引用既有 `wanshitong-v2-20260917` 题库，不复制问题正文或
私有证据。

- Demo20 分布固定为 6 条直接事实、4 条表格、4 条自然改写、3 条多轮或
  复合问题、3 条无证据或需澄清问题。
- Smoke12 固定覆盖确定性查找、自然正文、显式来源契约、子问题遗漏保护、
  真无证据和追问。
- Replay6 用于本阶段 F048 加 5 条代表题的真实候选环境回放。
- `question_sha256` 与四个源文件哈希用于防止执行时静默换题。

执行时必须使用 `run_wb08r03_candidate.py --case-id ...` 显式选择清单内题目，
不得省略 `--case-id` 触发 Full96。结果写入已忽略的
`evaluation/wanshitong/v2/results/`，不得提交私有 review 或 Trace 正文。
