# Evaluation V3

P08.5 在原有默认离线评测路径上增加 schema 3、检索阶段诊断、精确来源范围、
Evidence precision 和实际 Provider 调用计数。Runner 继续使用生产 P06 Revision
Builder 与 P07/P08.5 Retrieval 合约，输入只有生成的合成 DOCX。它不测量真实
Jina、Qwen 或远程 Reranker 的语义质量。

## Commands

在仓库根目录使用 Python 3.11 `.venv`：

```bash
.venv/bin/python scripts/dev.py eval-validate-dataset \
  --dataset evaluation/datasets/synthetic
.venv/bin/python scripts/dev.py eval-run \
  --dataset evaluation/datasets/synthetic \
  --profile configs/profiles/dev-offline.json \
  --lane offline-structural
.venv/bin/python scripts/dev.py eval-compare \
  --baseline evaluation/reports/<baseline-run-id> \
  --candidate evaluation/reports/<candidate-run-id>
.venv/bin/python scripts/dev.py eval-report \
  --run evaluation/reports/<run-id>
```

Run 目录独占创建，重复 run ID 或覆盖结果会失败。主机相关 timing 与 memory
结果默认不进入 Git；接受证据必须显式提升到版本化摘要。

## Split and selection rules

Loader 同时兼容历史 Case schema 2 和扩展 schema 3。当前合成数据集使用 schema
3，共 52 Case，并校验 logical scope、fixture 覆盖、case identity、group isolation
和 P08.5 各类别最小样本。重复 exact source text 没有 occurrence 或 structural
anchor 时，运行时标签解析失败。

`tuning` 标签只供有界单变量矩阵使用。选择接口拒绝非 tuning 报告，选定候选
随后只在 `holdout` 运行一次；两者指标绝不平均。

离线矩阵包括 baseline、Exact only、FTS5 only、deterministic dense-primary only、
Exact+FTS5、绕过 Rerank/Neighbor Expansion、Evidence caps、表格上下文移除和
256/320/384 Chunk 候选。Live dense-standby、Jina reranking 与 failover quality
仍为 BLOCKED，必须由独立授权、正预算和凭据的 run 验证。

## Output and safety

完整 Run 包含 canonical observations、structured errors、tuning metrics、选定
holdout metrics、ablation/gate report、selected config，以及脱敏 manifest 和
SHA-256 sidecar。Manifest 禁止 query/body、secret、vector、prompt、raw response
和 absolute path，并记录实际外部调用；离线值必须为空。

Retrieval 指标来自真实 Channel/Fusion/Rerank IDs；Evidence 指标来自最终发布
SourceSpan；Answer 指标来自回答、拒答与 Citation。Bootstrap interval 至少五个
样本才输出。更小切片标为 `insufficient_sample`，未执行能力不得转换为数值零。

## Quality boundary

离线通过只支持 deterministic structure、identity、revision/scope isolation、
指标算术、Citation、Evidence precision、拒答和回归检测。它不支持 Provider
semantics、limits、cost、production latency 或 remote failover quality 声明。
发布结果前阅读 `docs/public/search-quality-boundaries.md`。
