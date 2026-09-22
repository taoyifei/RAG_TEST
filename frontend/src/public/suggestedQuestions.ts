// 推荐问题仅用于引导提问；不保存评测题号、答案或检索规则。
export type SuggestedQuestionStyle = "SHORT" | "STANDARD" | "COMPOUND";

export interface SuggestedQuestion {
  id: string;
  question: string;
  // 仅用于推荐多样性，不作为后端来源过滤或授权标识。
  documentId: string;
  style: SuggestedQuestionStyle;
  topicKey: string;
  enabled: boolean;
}

export const SUGGESTED_QUESTIONS_REVISION = "2026-09-22-f01-r1";

export const SUGGESTED_QUESTIONS: readonly SuggestedQuestion[] = [
  {
    id: "sq-security-assessment-lead-time-01",
    question: "系统上线前的安全评估申请有哪些时间要求？",
    documentId: "DOCX-014",
    style: "SHORT",
    topicKey: "security-assessment",
    enabled: true,
  },
  {
    id: "sq-asset-depreciation-start-01",
    question: "固定资产折旧的起算规则是什么？",
    documentId: "DOCX-026",
    style: "SHORT",
    topicKey: "fixed-assets",
    enabled: true,
  },
  {
    id: "sq-rd-hours-record-01",
    question: "研发工时怎样记录和留痕？",
    documentId: "DOCX-003",
    style: "SHORT",
    topicKey: "rd-hours",
    enabled: true,
  },
  {
    id: "sq-design-document-confirmation-01",
    question: "业务团队收到设计文档后如何完成业务确认？",
    documentId: "DOCX-046",
    style: "SHORT",
    topicKey: "product-delivery",
    enabled: true,
  },
  {
    id: "sq-company-seal-application-01",
    question:
      "申请公司公章要从哪个系统入口发起，需要填写或上传什么，完整审批流程是什么？",
    documentId: "DOCX-023",
    style: "SHORT",
    topicKey: "seal-application",
    enabled: true,
  },
  {
    id: "sq-rd-outsourcing-budget-01",
    question: "研发外协成本使用时是否需要先有预算？",
    documentId: "DOCX-009",
    style: "SHORT",
    topicKey: "rd-outsourcing",
    enabled: true,
  },
  {
    id: "sq-procurement-response-deadline-01",
    question: "采购文件发布到应答截止有什么时间要求？",
    documentId: "DOCX-018",
    style: "SHORT",
    topicKey: "procurement",
    enabled: true,
  },
  {
    id: "sq-certification-scope-01",
    question: "外部认证管理办法的适用边界是什么？",
    documentId: "DOCX-010",
    style: "SHORT",
    topicKey: "employee-certification",
    enabled: true,
  },
  {
    id: "sq-rd-project-kickoff-01",
    question: "研发项目立项时需要准备哪些事项？",
    documentId: "DOCX-004",
    style: "SHORT",
    topicKey: "rd-project",
    enabled: true,
  },
  {
    id: "sq-ip-records-filing-01",
    question: "知识产权管理机构需要对哪些类型的文件资料进行备案？",
    documentId: "DOCX-012",
    style: "STANDARD",
    topicKey: "intellectual-property",
    enabled: true,
  },
  {
    id: "sq-performance-indicator-adjustment-01",
    question: "组织绩效考核指标调整需要经过哪些程序？",
    documentId: "DOCX-016",
    style: "STANDARD",
    topicKey: "performance-management",
    enabled: true,
  },
  {
    id: "sq-office-supplies-off-catalog-01",
    question: "哪些情况下可以采购电商平台或集采目录里没有的办公用品？",
    documentId: "DOCX-024",
    style: "STANDARD",
    topicKey: "office-procurement",
    enabled: true,
  },
  {
    id: "sq-rd-project-name-rules-01",
    question:
      "研发项目名称中是否需要同时包含项目内容和动作，并且不能使用非研发相关的动词？",
    documentId: "DOCX-007",
    style: "COMPOUND",
    topicKey: "rd-project-naming",
    enabled: true,
  },
  {
    id: "sq-technology-license-negotiation-01",
    question:
      "在技术授权谈判过程中，若谈判结果超出预定方案范围且双方无法达成一致，是否仍可继续推进授权流程？",
    documentId: "DOCX-011",
    style: "COMPOUND",
    topicKey: "technology-licensing",
    enabled: true,
  },
  {
    id: "sq-rd-direct-cost-scope-01",
    question:
      "研发活动直接消耗的材料、燃料和动力费用是否属于研究开发支出列支范围？",
    documentId: "DOCX-027",
    style: "COMPOUND",
    topicKey: "rd-costs",
    enabled: true,
  },
];
