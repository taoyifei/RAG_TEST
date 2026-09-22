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
    question: "上线安全评估要提前多久申请？",
    documentId: "DOCX-014",
    style: "SHORT",
    topicKey: "security-assessment",
    enabled: true,
  },
  {
    id: "sq-asset-depreciation-start-01",
    question: "固定资产从什么时候开始折旧？",
    documentId: "DOCX-026",
    style: "SHORT",
    topicKey: "fixed-assets",
    enabled: true,
  },
  {
    id: "sq-rd-hours-record-01",
    question: "研发工时怎么填、怎么留痕？",
    documentId: "DOCX-003",
    style: "SHORT",
    topicKey: "rd-hours",
    enabled: true,
  },
  {
    id: "sq-design-document-confirmation-01",
    question: "业务团队收到设计文档后怎么确认？",
    documentId: "DOCX-046",
    style: "SHORT",
    topicKey: "product-delivery",
    enabled: true,
  },
  {
    id: "sq-company-seal-application-01",
    question: "公司公章怎么申请？",
    documentId: "DOCX-023",
    style: "SHORT",
    topicKey: "seal-application",
    enabled: true,
  },
  {
    id: "sq-rd-outsourcing-budget-01",
    question: "研发外协费用需要先有预算吗？",
    documentId: "DOCX-009",
    style: "SHORT",
    topicKey: "rd-outsourcing",
    enabled: true,
  },
  {
    id: "sq-procurement-response-deadline-01",
    question: "采购文件发布到应答截止至少要几天？",
    documentId: "DOCX-018",
    style: "SHORT",
    topicKey: "procurement",
    enabled: true,
  },
  {
    id: "sq-certification-scope-01",
    question: "哪些认证不适用外部认证管理办法？",
    documentId: "DOCX-010",
    style: "SHORT",
    topicKey: "employee-certification",
    enabled: true,
  },
  {
    id: "sq-rd-project-kickoff-01",
    question: "研发项目立项要准备什么？",
    documentId: "DOCX-004",
    style: "SHORT",
    topicKey: "rd-project",
    enabled: true,
  },
  {
    id: "sq-ip-records-filing-01",
    question: "知识产权管理机构要备案哪些文件资料？",
    documentId: "DOCX-012",
    style: "STANDARD",
    topicKey: "intellectual-property",
    enabled: true,
  },
  {
    id: "sq-performance-indicator-adjustment-01",
    question: "组织绩效考核指标调整要走哪些程序？",
    documentId: "DOCX-016",
    style: "STANDARD",
    topicKey: "performance-management",
    enabled: true,
  },
  {
    id: "sq-office-supplies-off-catalog-01",
    question: "什么情况下可以采购电商平台或集采目录外的办公用品？",
    documentId: "DOCX-024",
    style: "STANDARD",
    topicKey: "office-procurement",
    enabled: true,
  },
  {
    id: "sq-rd-project-name-rules-01",
    question: "研发项目名称必须包含哪些内容，哪些非研发动词不能使用？",
    documentId: "DOCX-007",
    style: "COMPOUND",
    topicKey: "rd-project-naming",
    enabled: true,
  },
  {
    id: "sq-prebid-publicity-deadline-01",
    question: "标前公示截止日必须是工作日吗，至少要公示几天？",
    documentId: "DOCX-020",
    style: "COMPOUND",
    topicKey: "procurement-publicity",
    enabled: true,
  },
  {
    id: "sq-rd-direct-cost-scope-01",
    question: "研发直接消耗的材料、燃料和动力费用能计入研发支出吗？",
    documentId: "DOCX-027",
    style: "COMPOUND",
    topicKey: "rd-costs",
    enabled: true,
  },
];
