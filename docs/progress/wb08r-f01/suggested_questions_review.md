# WB-08R F01 短问推荐题库审核表

状态：`F01_R2_8289_CANDIDATE_ACCEPTED`

题库 revision：`2026-09-23-f01-r2`

审核日期：2026-09-23

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
| `sq-security-assessment-lead-time-01` | 上线安全评估要提前多久申请？ | SHORT | security-assessment | 是，DOCX-014 已登记 | 8289 首问可用 | 是 | 保留“上线前”和提前量含义 |
| `sq-asset-depreciation-start-01` | 固定资产从什么时候开始折旧？ | SHORT | fixed-assets | 是，DOCX-026 已登记 | 8289 首问可用 | 是 | 只问起算，不混入折旧年限 |
| `sq-rd-hours-record-01` | 研发工时怎么填、怎么留痕？ | SHORT | rd-hours | 是，DOCX-003 已登记 | 长会话超时；新会话首问可用 | 是 | 同时保留填写和留痕两个要求 |
| `sq-design-document-confirmation-01` | 业务团队收到设计文档后怎么确认？ | SHORT | product-delivery | 是，DOCX-046 已登记 | 新会话首问仍仅回答时限 | 否 | 缺少所问确认步骤；窄化试问仍带无依据提示 |
| `sq-company-seal-application-01` | 公司公章怎么申请？ | SHORT | seal-application | 是，DOCX-023 已登记 | 长会话拒答；新会话首问可用 | 是 | 新的宽问，不视为旧复合题的同义编辑 |
| `sq-rd-outsourcing-budget-01` | 研发外协费用需要先有预算吗？ | SHORT | rd-outsourcing | 是，DOCX-009 已登记 | 8289 首问可用 | 是 | 保留预算先后限定 |
| `sq-procurement-response-deadline-01` | 采购文件发布到应答截止至少要几天？ | SHORT | procurement | 是，DOCX-018 已登记 | 8289 首问可用 | 是 | 保留“至少”限定 |
| `sq-certification-scope-01` | 哪些认证不适用外部认证管理办法？ | SHORT | employee-certification | 是，DOCX-010 已登记 | 8289 首问可用 | 是 | 保留否定边界 |
| `sq-rd-project-kickoff-01` | 研发项目立项要准备什么？ | SHORT | rd-project | 是，DOCX-004 已登记 | 长会话偏题；新会话首问可用 | 是 | 只问立项准备，不扩成全流程 |
| `sq-ip-records-filing-01` | 知识产权管理机构要备案哪些文件资料？ | STANDARD | intellectual-property | 是，DOCX-012 已登记 | 长会话与新会话均拒答 | 否 | 当前索引未确认对应资料，停用而不删除题面 |
| `sq-performance-indicator-adjustment-01` | 组织绩效考核指标调整要走哪些程序？ | STANDARD | performance-management | 是，DOCX-016 已登记 | 长会话漏步骤；新会话首问可用 | 是 | 不预设是否可以绕过审批 |
| `sq-office-supplies-off-catalog-01` | 什么情况下可以采购电商平台或集采目录外的办公用品？ | STANDARD | office-procurement | 是，DOCX-024 已登记 | 8289 首问可用 | 是 | 保留两个目录边界 |
| `sq-rd-project-name-rules-01` | 研发项目名称必须包含哪些内容，哪些非研发动词不能使用？ | COMPOUND | rd-project-naming | 是，DOCX-007 已登记 | 新会话首问仍漏必须项 | 否 | 保留原限定供人工提问，不主动推荐残缺回答 |
| `sq-prebid-publicity-deadline-01` | 标前公示截止日必须是工作日吗，至少要公示几天？ | COMPOUND | procurement-publicity | 是，DOCX-020 已登记 | 新会话首问答对但附带无依据提示 | 否 | 两种改写仍拒答或自相矛盾，停用复合题 |
| `sq-prebid-publicity-min-days-01` | 标前公示时间不少于几日？ | SHORT | procurement-publicity | 是，DOCX-020 已登记 | 新会话首问可用且引用原文 | 是 | 新题意、新 ID；不含工作日部分 |
| `sq-rd-direct-cost-scope-01` | 研发直接消耗的材料、燃料和动力费用能计入研发支出吗？ | COMPOUND | rd-costs | 是，DOCX-027 已登记 | 8289 首问可用 | 是 | 保留三类费用对象 |

## 本轮验收记录

本地选择、组件和请求合同验证完成后，仅在 8289 候选环境试问本批题。
回答不佳的题停用，不修改全局检索、重排或生成算法。停用只影响主动推荐，
不阻止用户手工提问。首批 15 条中的 11 条在独立新会话可用；4 条停用，
新增 1 条已在旧候选上独立试问并取得直接引用，因此 r2 保持 12 条启用。

### 2026-09-23 候选环境试问

- 8289 候选镜像源码为 `a6345d6b518234fe3baf1a2ce5150956fafebeee`；部署门禁
  修正提交为 `138c9fa99e4b857d0b668e1a29f38970ca10676d`。已启用 SSO 的
  原值继承通过渲染配置和创建容器两层门禁，二者的缺失键、语义差异、挂载差异、
  未分类差异计数均为 0。
- 60 服务器的新候选容器处于 `healthy` 且重启数为 0。经 54 转发的
  `http://10.242.180.54:8289/kb/live` 和 `/kb/ready` 均返回 200。
  8289 前端资源 `index-BMlqd3Qt.js` 包含首批 15/15 个稳定 ID 和对应题面。
- 浏览器经 SSO 成功登录 8289。首屏显示 5 条推荐，比例为 3 SHORT / 1 STANDARD /
  1 COMPOUND；点击卡片的气泡与 `/kb/api/public/chat` 请求 `query` 一致，请求体
  仅包含 `conversation_id` 和 `query`，每次点击只提交一次。
- 原 15 条启用题均通过卡片真实提交；针对长会话中异常的 8 条进行独立新会话
  首问复测，另对 5 种窄化或替代题面做有限试问。独立新会话复测排除了部分
  长会话上下文干扰，但不将一次可用视作长期质量保证。
- 保留“必须、否定、工作日”的原题面与请求合同；质量不稳定的复合题只从主动
  推荐中停用。新启用短问题面在候选环境返回直接依据。
- 本次试问中 15 个异常请求的 History + Operational Trace 支持包另存于用户指定的
  本机日志目录；包内 15 份问答正文与 15 份 Trace 均可用，并含校验清单。

### r2 候选验收与部署修正

- r2 候选镜像源码为 `c13ae4ebdd8469f06b9ae891a54d1439b01d4286`，镜像
  `sha256:269636bfc7b9fa1c331e4dcb55ad89eed8e1bf091f31e8421ff790ccbc9cb63b`。
  新候选单独挂载数据、日志与诊断目录；两份复制后的 SQLite 数据库
  `quick_check` 均为 `ok`。渲染与创建门禁均为 `ready=true`，缺失键、语义差异、
  挂载差异和未分类差异均为 0。
- 切换时发现固定 Compose project 仍识别改名后的旧 8289 容器并重建，旧容器级
  回滚点因而丢失。新容器已在创建门禁通过后直接启动，当前 `healthy`、重启数 0，
  8289 的 `/live` 与 `/ready` 可用。部署脚本已修正为按独立候选根目录派生
  Compose project，避免以后再重建改名的旧候选；本次旧数据根目录仍保留。
- 浏览器沿原 SSO 会话登录新候选成功。前三组换题覆盖全部 12 条启用题，停用题
  未主动出现；首屏仍为 3 SHORT / 1 STANDARD / 1 COMPOUND。新题点击后气泡、
  `query` 完全一致，请求体仍只有 `conversation_id` 与 `query`，且回答引用直接原文。
  本次候选验收不代表长期问答质量或 18288 正式发布。
- 相关本地验证：38 项前端单测、lint、类型检查、构建，以及桌面与移动两条
  Playwright 用例均通过。Linux 浏览器初次因缺少系统库未启动；补齐运行依赖后
  原用例重跑通过，不将环境失败计作产品通过。
- 验收后已按精确 ID 删除停用的旧 8289 回滚容器，并删除 60 服务器上不再被
  容器引用的 `a6345d6` 候选镜像；本机旧版和新版 8289 构建镜像均已清理。
  旧候选数据目录保留，传输归档与本次浏览器快照已清理。旧版镜像
  `sha256:96f1aa48eed69de4a98f48af6f4fa89ebf5b35a6392c30697ee855b105f5ecd9`
  仍被运行中的正式服务引用，因此明确未删除。本批 F01 在 60 服务器上仅保留
  运行中的 `c13ae4e` 候选镜像；18288 的正式服务未由本任务变更。
