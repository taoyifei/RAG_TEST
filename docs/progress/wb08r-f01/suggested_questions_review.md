# WB-08R F01 短问推荐题库审核表

状态：`CANDIDATE_DEPLOYED_LIVE_AUTH_BLOCKED`

题库 revision：`2026-09-22-f01-r1`

审核日期：2026-09-22

开发基线：`feature/wanshitong@4672760b39105ccfb582e9615bd02d94f1e4f5b2`

## 维护边界

- `question` 是卡片、聊天气泡和请求 `query` 共用的唯一题面。
- `documentId` 只用于推荐多样性，不是来源过滤或授权标识。
- 本表不记录答案、Expected Source 白名单、私有 Trace 或真实用户问题。
- 旧公共题面已从首批入口移出；独立的冻结评测与回归数据保持不变，且不导入运行时。
- 同义编辑保留稳定 ID；题意明显改变时必须创建新 ID，并递增题库 revision。

## 首批题目

| ID | 题面 | style | topicKey | 资料存在性 | 最近试问 | 启用 | 停用理由/备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `sq-security-assessment-lead-time-01` | 上线安全评估要提前多久申请？ | SHORT | security-assessment | 是，DOCX-014 已登记 | 待本修订 8289 有限试问 | 是 | 保留“上线前”和提前量含义 |
| `sq-asset-depreciation-start-01` | 固定资产从什么时候开始折旧？ | SHORT | fixed-assets | 是，DOCX-026 已登记 | 待本修订 8289 有限试问 | 是 | 只问起算，不混入折旧年限 |
| `sq-rd-hours-record-01` | 研发工时怎么填、怎么留痕？ | SHORT | rd-hours | 是，DOCX-003 已登记 | 待本修订 8289 有限试问 | 是 | 同时保留填写和留痕两个要求 |
| `sq-design-document-confirmation-01` | 业务团队收到设计文档后怎么确认？ | SHORT | product-delivery | 是，DOCX-046 已登记 | 待本修订 8289 有限试问 | 是 | 保留责任主体和收到文档的前提 |
| `sq-company-seal-application-01` | 公司公章怎么申请？ | SHORT | seal-application | 是，DOCX-023 已登记 | 待本修订 8289 有限试问 | 是 | 新的宽问，不视为旧复合题的同义编辑 |
| `sq-rd-outsourcing-budget-01` | 研发外协费用需要先有预算吗？ | SHORT | rd-outsourcing | 是，DOCX-009 已登记 | 待本修订 8289 有限试问 | 是 | 保留预算先后限定 |
| `sq-procurement-response-deadline-01` | 采购文件发布到应答截止至少要几天？ | SHORT | procurement | 是，DOCX-018 已登记 | 待本修订 8289 有限试问 | 是 | 保留“至少”限定 |
| `sq-certification-scope-01` | 哪些认证不适用外部认证管理办法？ | SHORT | employee-certification | 是，DOCX-010 已登记 | 待本修订 8289 有限试问 | 是 | 保留否定边界 |
| `sq-rd-project-kickoff-01` | 研发项目立项要准备什么？ | SHORT | rd-project | 是，DOCX-004 已登记 | 待本修订 8289 有限试问 | 是 | 只问立项准备，不扩成全流程 |
| `sq-ip-records-filing-01` | 知识产权管理机构要备案哪些文件资料？ | STANDARD | intellectual-property | 是，DOCX-012 已登记 | 待本修订 8289 有限试问 | 是 | 保留责任主体 |
| `sq-performance-indicator-adjustment-01` | 组织绩效考核指标调整要走哪些程序？ | STANDARD | performance-management | 是，DOCX-016 已登记 | 待本修订 8289 有限试问 | 是 | 不预设是否可以绕过审批 |
| `sq-office-supplies-off-catalog-01` | 什么情况下可以采购电商平台或集采目录外的办公用品？ | STANDARD | office-procurement | 是，DOCX-024 已登记 | 待本修订 8289 有限试问 | 是 | 保留两个目录边界 |
| `sq-rd-project-name-rules-01` | 研发项目名称必须包含哪些内容，哪些非研发动词不能使用？ | COMPOUND | rd-project-naming | 是，DOCX-007 已登记 | 待本修订 8289 有限试问 | 是 | 保留必须项与否定项 |
| `sq-prebid-publicity-deadline-01` | 标前公示截止日必须是工作日吗，至少要公示几天？ | COMPOUND | procurement-publicity | 是，DOCX-020 已登记 | 待本修订 8289 有限试问 | 是 | 保留工作日和最短时长两个限定 |
| `sq-rd-direct-cost-scope-01` | 研发直接消耗的材料、燃料和动力费用能计入研发支出吗？ | COMPOUND | rd-costs | 是，DOCX-027 已登记 | 待本修订 8289 有限试问 | 是 | 保留三类费用对象 |

## 本轮验收记录

本地选择、组件和请求合同验证完成后，只在 8289 候选环境试问以上启用题。
回答不佳时优先调整题面或停用单题，不修改全局检索、重排或生成算法；本节在
真实试问后补充结果与最终启用状态。

### 2026-09-23 候选环境与阻塞

- 8289 候选镜像源码为 `a6345d6b518234fe3baf1a2ce5150956fafebeee`；部署门禁
  修正提交为 `138c9fa99e4b857d0b668e1a29f38970ca10676d`。已启用 SSO 的
  原值继承通过渲染配置和创建容器两层门禁，二者的缺失键、语义差异、挂载差异、
  未分类差异计数均为 0。
- 60 服务器的新候选容器处于 `healthy` 且重启数为 0。经 54 转发的
  `http://10.242.180.54:8289/kb/live` 和 `/kb/ready` 均返回 200。
  8289 前端资源 `index-BMlqd3Qt.js` 包含首批 15/15 个稳定 ID 和对应题面。
- 浏览器从 8289 正常跳转至研发管理系统 SSO。现有服务器账号在该 SSO 的
  `/prod-api/login` 返回业务错误“用户不存在/密码错误”；尚未取得可用的 SSO
  测试身份，因此未进入推荐首屏，也未提交任何一条真实试问。
- 旧 8289 容器已停止并保留回滚；生产 18288 容器及其代理保持原容器身份、
  `healthy`、重启数为 0。真实交互验收、逐题质量判定和过期镜像清理均待
  SSO 身份可用后执行，本记录不将静态资源核对视为真实质量通过。
