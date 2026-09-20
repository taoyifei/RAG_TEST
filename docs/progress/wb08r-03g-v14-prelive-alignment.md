# WB08R-03G V14 真实测试前一致性审计

状态：**OFFLINE_ALIGNED / LIVE_NOT_RUN**。本文只证明 V14 实现合同和相关离线门禁
已经闭合，不把单元测试数量当作真实问答质量结论。生产、8289 真实问答、Preflight-16
和 A～F 在本文形成时均未运行。

审计基线为 V13 最终提交 `332dd46a7da1b7d4be373cbd28628411a2b6c780`，目标为
《WB08R03_V14_统一问答计划与执行内核实施方案_332dd46.md》。附件中的
`acceptance_cases.json` 是验收定义，不是既有通过结果。

## 结论

新 03 路径已经按 V14 收敛为：

1. 在 QueryAnalyzer、Planner、Rewriter 前解析显式来源，保存原始/NFKC 跨度、角色、
   文档版本许可和稳定业务视图；两份文档比较按子句签发不相交的来源范围。
2. `CompiledAnswerPlan` 是回答义务、物理选择、必要成员、正向依赖和物理任务的唯一
   权威对象。QueryPlan 只保留检索与兼容建议。
3. `reduce_plan_coverage()` 只读取冻结计划与受检产物，不读取原问、不导入语义解析器、
   不调用模型，也不从最终 refs 反推 required set。
4. `executor.py` 调度 D/G 物理任务。D 路径重新核对来源、版本、事实身份、跨度和依赖后
   直接渲染，回答阶段生成与语义复核调用均为零；G 路径由执行内核限制为最多两个真实
   生成批和两个互不重叠的真实复核批。
5. 新路径只接受轻量 Wire 与实际发送包，不允许失败后回退 legacy 协议、摘录兜底或
   自动 repair。旧兼容入口仍保留，但不是 V14 失败兜底。
6. 固定 24 个假想 Claim 的复核预检已从生产链路删除。24 只作为真实响应 schema 的
   容量上限；6144、1536、既有 deadline 和共享 LLM 并发上限 4 均未提高。
7. 计划、Trace、缓存身份包含新 schema/policy/source scope 版本；活动文档版本变化会
   使执行身份和缓存失效。

静态搜索确认生产改动中不存在 F024、N031、具体文件名、具体业务行名、内网地址或凭据
特判；不存在 `semantic_review_preflight_tokens`、
`SEMANTIC_REVIEW_PREFLIGHT_BUDGET_EXCEEDED`，也没有提高输入上限、增加 timeout 或恢复
新路径 repair。

## 本轮严格审计中发现并在真实测试前修正的偏差

- 文档比较最初仍可能把两份来源合成全局可借用集合；现已用 `SOURCE_A/SOURCE_B` 和
  逐 Atom 文档身份拆开。
- 危险限定最初可能从后一子句传播到前一义务；现按 Atom 自己的业务片段冻结限定。
- `grounded.py` 最初仍自行调度 G 批次；现委托 `executor.py`，Grounded 只提供生成、
  绑定、复核和失败记录回调。
- 唯一短目标与完整目标虽命中同一事实，但选择摘要曾包含绑定依据而发生漂移；现选择
  身份只由物理事实、活动版本、正向依赖和来源作用域构成，绑定依据仅作审计字段。
- V14 回放脚本最初只允许 `c1-c3`；现支持稳定递增的正整数候选编号，并在 SAFE 证据中
  保存前置来源解析身份，仍不保存正文。

以上均属于“实现尚未严格落到 V14”的问题，因此按用户授权在真实测试前修正。后续若
严格实现的 8289 真实测试出现新问题，不再自行修改算法，只保存证据、记录 P0、回滚并
精确清理候选镜像和临时文件，然后提交和推送。

## 40 项验收定义与证据入口

下表的“离线”只表示对应合同/反例已执行；需要真实模型或并发运行的项目仍标为“待
8289”，不能由离线结果替代。

| ID | 当前证据入口 | 状态 |
|---|---|---|
| S01 | `test_explicit_source_field_lookup_freezes_only_requested_fact` | 离线通过 |
| S02 | `test_original_explicit_source_wins_planner_qualifier`、`test_explicit_source_is_resolved_before_planner_receives_business_query` | 离线通过 |
| S03 | `test_unresolved_source_never_becomes_open` | 离线通过 |
| S04 | `test_same_name_source_is_ambiguous_and_has_no_allowed_document` | 离线通过 |
| S05 | `test_source_context_is_frozen_before_business_analysis`、`test_authority_title_words_do_not_create_answer_qualifiers` | 离线通过 |
| S06 | `test_referenced_title_is_not_removed_with_source_owner` | 离线通过 |
| S07 | `test_catalog_only_source_and_real_body_are_a_paired_boundary`、三项 catalog body 拒绝回归 | 离线通过 |
| S08 | 与 S07 成对的真实正文正例及带“模板”标题的精确解析 | 离线通过 |
| P01 | `test_explicit_source_field_lookup_freezes_only_requested_fact` | 离线通过 |
| P02 | `test_explicit_column_variant_keeps_same_selection_identity` | 离线通过 |
| P03 | `test_unique_short_target_and_full_target_keep_selection_identity` | 离线通过 |
| P04 | `test_duplicate_schema_candidates_do_not_form_deterministic_binding` | 离线通过 |
| P05 | `test_strict_temporal_modality_is_not_erased_or_auto_satisfied` | 离线通过 |
| P06 | `test_explicit_source_qualifier_can_close_temporal_obligation` | 离线通过 |
| P07 | `test_two_fields_keep_two_obligations_but_share_one_physical_task` | 离线通过 |
| P08 | `test_two_roles_in_different_rows_keep_disjoint_obligations` | 离线通过 |
| C01 | `test_deterministic_execution_and_coverage_ignore_sibling_column` | 离线通过 |
| C02 | `test_missing_second_field_stays_missing_after_first_is_published` | 离线通过 |
| C03 | `test_structured_list_full_quote_does_not_hide_missing_member` | 离线通过 |
| C04 | `test_structured_ordinal_requires_only_requested_member` | 离线通过 |
| C05 | `test_split_chunks_of_same_source_node_are_one_member` | 离线通过 |
| C06 | `test_source_list_ending_with_etc_is_complete_only_for_that_source` | 离线通过 |
| C07 | `test_coverage_rejects_changed_plan_or_selection_identity` | 离线通过 |
| D01 | `test_service_deterministic_path_calls_no_generation_or_review` | 离线通过 |
| D02 | `test_value_and_header_without_physical_row_label_cannot_form_fact` | 离线通过 |
| D03 | `test_binding_rejects_real_but_wrong_document` | 离线通过 |
| D04 | `test_supported_review_cannot_upgrade_incomplete_table_relation` | 离线通过 |
| D05 | `test_mixed_plan_keeps_deterministic_fact_when_generation_fails` | 离线通过 |
| B01 | `test_multi_atom_fact_catalog_sends_each_source_and_fact_once` | 离线通过 |
| B02 | `test_actual_claims_replace_fixed_semantic_preflight_shells` | 离线通过 |
| B03 | `test_two_actual_generation_batches_are_disjoint` | 离线通过 |
| B04 | 结构化列表与混合计划的 `RESOURCE_LIMITED` 回归 | 离线通过 |
| B05 | 四种 structured-output 组合的实际 schema/message 预算回归 | 离线通过 |
| B06 | `test_two_actual_review_batches_are_disjoint` | 离线通过 |
| R01 | `test_concurrent_attempt_registries_do_not_share_s1`；真实并发 4 | 离线身份通过；并发待 8289 |
| R02 | `test_explicit_source_version_change_invalidates_execution_identity` | 离线通过 |
| R03 | 两项取消期间不发布复核结果回归；真实并发取消 | 离线通过；并发待 8289 |
| T01 | `test_runtime_path_observation_does_not_require_a_gold_case` 保留 `NEEDS_TRUTH_REVIEW` | 离线通过 |
| T02 | `test_strict_compiled_path_rejects_legacy_protocol_without_fallback`、新 Wire HTTP 回归 | 离线通过；真实 Wire 待 8289 |
| T03 | `test_candidate_reorder_and_alias_change_keep_selection_identity` | 离线通过 |

## 已执行门禁

仅运行 03 问答及 V14 候选证据脚本直接相关的测试，没有运行全仓库、前端、索引重建、
迁移或其它功能测试。

- V14 全部改动模块的专项 pytest：`399 passed in 14.99s`。
- 变更范围 Ruff format：`31 files already formatted`。
- 变更范围 Ruff：`All checks passed`。
- 18 个相关生产源码/脚本的 mypy：`Success: no issues found`。
- 新执行内核、计划编译器、前置来源解析独立导入：通过。
- `git diff --check`：通过。

## 下一门禁

以本文对应的干净提交构建一个新 V14 候选，仅部署到 60 服务器的
`127.0.0.1:8289`。先冻结代码、镜像、运行树、配置、活动索引、数据集与 runner，确认
生产身份前后不变，再运行五组 V14 真实回放并作人工事实评审。

若出现新的真实功能问题，按用户要求停止修改，记录详细 P0，保留唯一私有回放证据，
将 8289 回滚到 `wb08r03f-bd00c56`，精确清理本次候选镜像和临时文件，不执行全局
prune，然后提交并推送。若五组全部通过，才继续同版本 Preflight-16 与 A～F；生产替换
仍需用户另行授权。
