# WB08R-03R2 冻结回归审计集

本目录只用于候选验收。从 `evaluation/wanshitong/v2/` 已冻结文件复制原问题，保持 `case_id`、问题摘要、上下文问题与预期行为。`manifest.json` 锁定本目录和来源文件的 SHA-256。

`audit_categories` 是需要复核的失败类别，不是标准答案或自动判题规则。特别是 `UNSUPPORTED_ACCEPTED_INFERENCE` 与 `FALSE_LIMITED` 仍需结合来源证据人工核对，不能仅凭引用数量作结论。这里不保存答案正文、引用摘录或业务规则。

生产代码、Prompt、索引、前端均不得读取此目录；`.dockerignore` 排除整个 `evaluation/wanshitong/`。如需改变原问题或上下文，应另建版本并重新冻结身份。

`baseline-safe-summary.json` 保存旧候选的无正文基线。旧 Trace 缺少 accepted Claim 数和完整性记录，相应指标写 `NOT_OBSERVED`，不能用公共 Claim 事件数代替。

`gate-selections.json` 整理 Context、Evidence Ownership、Ownership、Claims 和 AnswerShape 的既有冻结题选择。它只确认 case 身份与原始期望行为。Context-12 还缺 2 条多对象澄清和 1 条来源冲突的冻结输入；其余 Gate 仍缺来源跨度、结构组、Claim 蕴含等人工真值，状态保持 `NOT_OBSERVED`。这些选择不得被解读为真实 Gate 已通过。
