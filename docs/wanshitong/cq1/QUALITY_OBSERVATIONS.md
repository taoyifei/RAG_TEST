# CQ1 支持检查资格观察

记录日期：2026-09-25。此处只记录冻结合成题的标签、模型判定和聚合结果；受控原文、模型完整响应与服务凭据不进入 Git。

## 资格集与真实配置

- 冻结输入：`tests/fixtures/cq1_support_qualification.json`，SHA-256 `08c11337f749531daf74809d6d1e1b0c0030742c731a53ee00c5f1a539b53eed`，10 个合成正负例。其标签由题面与原文先行确定，不由模型自身解释决定。
- 实测接口：8289 当前配置所指的 `Qwen/Qwen3-8B-AWQ` 兼容 API `http://10.242.180.54:18000/v1`；`GET /models` 返回 `max_model_len=8192`。资格调用使用 `temperature=0`、`max_tokens=2680`、`disable_thinking=false`，顺序执行。此为直接模型资格检查，不是 8289 应用回放，也不证明在线服务使用了新代码。
- 脱敏汇总对应的受控本地响应文件：`artifacts/cq1/qualification/aligned-8289.ndjson`，SHA-256 `25dfe7b77f31d797039f8730123eab71f7b6c0a96998677c9970c1372e5f0e21`。该文件不提交，因为包含合成题完整响应。所有 10 次调用的 `finish_reason=stop`，JSON 基本格式可解析。

| Case | 预先判定 | 模型判定 | 结果 |
| --- | --- | --- | --- |
| V01_DIRECT | supported | supported | 正确 |
| V02_SPECIFIC_ENTITY | unsupported | supported | **误接受** |
| V03_REVERSED_EXCEPTION | contradicted | supported | **误接受** |
| V04_AMOUNT_RATIO | unsupported | unsupported | 正确 |
| V05_WORKING_DAYS | contradicted | contradicted | 正确 |
| V06_TEMPLATE_EXAMPLE | unsupported | supported | **误接受** |
| V07_COMPOSITE_HALF | unsupported | unsupported | 正确 |
| V08_IRRELEVANT_ANSWER | unsupported | supported | **误接受** |
| V09_WRONG_TABLE_ROW | contradicted | contradicted | 正确的否定判定；顶层 coverage 为 complete，仍需合同约束 |
| V10_MISSING_CITATION | supported | supported | 正确 |

共 **4/10 误接受、6/10 判定正确、0/10 基本格式失败**。四个误接受各自返回了当前 `c1` 材料中唯一存在的逐字摘录；把这四份原始审核输出离线交给当前确定性发布器及合成来源跨度，四次均得到 `GROUNDED_ANSWER`。因此仅靠句柄、原文逐字匹配和位置约束无法补救此类语义错误。该离线回放不代表真实企业题的错误率。

按 `02_Codex_CQ1总控执行指令.txt` 的 8B 能力停止条款与 `03_测试矩阵与验收口径.md` 的 C18/P0 门禁，**当前审核器不具备可信事实准出资格**。没有继续叠加判官、改写业务关键词或关闭核查来提高答复数。严格 v2 开关保持默认关闭；本轮未发布企业事实、未做候选部署或 72+20 回放。

## 尚未取得的质量证据

原 60 场景 72 轮与冻结新增 20 场景的同口径重评、逐原文人工审阅、完整率/部分率/不足率/不必要拒答率、事实引用支持率、首个可信正文延迟、四独立会话及同会话并发，都未执行。上述数值不能由离线单元测试或旧 A/B AI 初评推断。模板六层事实见 `TEMPLATE_DATA_AUDIT.md`。
