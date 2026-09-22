// 推荐问题仅用于引导提问；不保存评测题号、答案或检索规则。
export interface SuggestedQuestion {
  documentId: string;
  question: string;
}

export const SUGGESTED_QUESTIONS: readonly SuggestedQuestion[] = [
  {
    documentId: "DOCX-001",
    question: "项目负责人在使用科研经费时需要遵守哪些具体规定？",
  },
  {
    documentId: "DOCX-001",
    question: "哪些情况下项目经费不得用于支付个人或家庭费用？",
  },
  {
    documentId: "DOCX-002",
    question:
      "新业务、新技术试验工作中，哪些单位被明确划分为试验需求单位、试验管理单位、试验承担单位及试验配合单位？",
  },
  {
    documentId: "DOCX-002",
    question:
      "试验承担单位在制定技术方案时，需要与哪些单位共同进行评审，并确保方案的安全性与可行性？",
  },
  {
    documentId: "DOCX-003",
    question: "研发工时怎样记录和留痕？",
  },
  {
    documentId: "DOCX-003",
    question: "如何确保研发工时统计的准确性与合规性？",
  },
  {
    documentId: "DOCX-003",
    question: "哪些情况下的出差不计入研发工时？",
  },
  {
    documentId: "DOCX-004",
    question: "研发项目立项时需要准备哪些事项？",
  },
  {
    documentId: "DOCX-004",
    question:
      "当研发项目涉及外部合作时，是否需要对合作的必要性和计划合理性进行论证？",
  },
  {
    documentId: "DOCX-005",
    question: "哪些类型的项目被归类为战略研发项目？",
  },
  {
    documentId: "DOCX-005",
    question: "哪些情况下研发项目变更需要提请至本单位公司领导决策？",
  },
  {
    documentId: "DOCX-006",
    question:
      "除了基础研究外，中国移动研发活动还包含哪些与重大战略和需求相关的技术研究类型？",
  },
  {
    documentId: "DOCX-006",
    question:
      "哪些情况属于非研发用途的成品直接采购，且不适用于研发活动判断识别清单？",
  },
  {
    documentId: "DOCX-007",
    question:
      "研发项目命名中'动作'部分是否允许使用'安装、维护、运维、管理、支持、支撑、服务'等动词？",
  },
  {
    documentId: "DOCX-007",
    question:
      "研发项目名称中是否需要同时包含项目内容和动作，并且不能使用非研发相关的动词？",
  },
  {
    documentId: "DOCX-007",
    question:
      "研发项目名称是否可以包含特殊符号，或者是否允许使用超过30字的长度？",
  },
  {
    documentId: "DOCX-008",
    question: "联合研发费预算科目设立的目的是什么？",
  },
  {
    documentId: "DOCX-008",
    question: "如果联合研发课题未纳入年度预算方案，是否可以直接申请新增预算？",
  },
  {
    documentId: "DOCX-009",
    question: "研发外协成本使用时是否需要先有预算？",
  },
  {
    documentId: "DOCX-009",
    question: "研发外协服务人员的日常管理有哪些规定？",
  },
  {
    documentId: "DOCX-009",
    question:
      "当研发外协费用同时包含硬件开发和人力外协时，外协费总比例是否可以超过40%？",
  },
  {
    documentId: "DOCX-010",
    question: "外部认证管理办法的适用边界是什么？",
  },
  {
    documentId: "DOCX-010",
    question: "员工在申请认证时需要满足哪些与绩效相关的条件？",
  },
  {
    documentId: "DOCX-011",
    question:
      "在技术授权谈判过程中，若谈判结果超出预定方案范围且双方无法达成一致，是否仍可继续推进授权流程？",
  },
  {
    documentId: "DOCX-012",
    question: "知识产权管理机构需要对哪些类型的文件资料进行备案？",
  },
  {
    documentId: "DOCX-012",
    question: "当员工的个人智力劳动成果需要确认时，应向哪个机构提交书面说明？",
  },
  {
    documentId: "DOCX-013",
    question: "在岗技术革新奖励的申报成果需要满足哪些具体条件？",
  },
  {
    documentId: "DOCX-013",
    question: "哪些情况下在岗技术革新奖励的申报会被取消资格？",
  },
  {
    documentId: "DOCX-014",
    question: "系统上线前的安全评估申请有哪些时间要求？",
  },
  {
    documentId: "DOCX-014",
    question:
      "系统负责人需要确保第三方合作人员在离岗时完成哪些具体的工作交接事项？",
  },
  {
    documentId: "DOCX-014",
    question:
      "当系统负责人发现账号异常使用时，是否需要向公安、网信、工信等部门报告？",
  },
  {
    documentId: "DOCX-015",
    question: "授权用户在使用电子资源时应遵守哪些规定？",
  },
  {
    documentId: "DOCX-015",
    question: "哪些行为会被视为超出合理使用范围并可能导致电子资源访问被终止？",
  },
  {
    documentId: "DOCX-015",
    question: "哪些情况下授权用户不得将个人网络账号提供给非授权用户使用？",
  },
  {
    documentId: "DOCX-016",
    question: "部门组织绩效考核指标如何管理？",
  },
  {
    documentId: "DOCX-016",
    question: "组织绩效指标设计需要考虑哪些依据？",
  },
  {
    documentId: "DOCX-016",
    question: "组织绩效考核指标调整需要经过哪些程序？",
  },
  {
    documentId: "DOCX-017",
    question:
      "需求部门在进行采购决策时，是否需要向分管院领导及常务副院长汇报并获得批准？",
  },
  {
    documentId: "DOCX-017",
    question: "哪些类型的采购项目可以不经过招标程序直接与供应商签约？",
  },
  {
    documentId: "DOCX-018",
    question: "采购文件发布到应答截止有什么时间要求？",
  },
  {
    documentId: "DOCX-018",
    question:
      "采购经办人和需求部门经办人共同编制采购文件后，需要经过谁的审核才能定稿？",
  },
  {
    documentId: "DOCX-018",
    question: "谈判小组的专家成员是否必须全部从广东公司采购专家库中产生？",
  },
  {
    documentId: "DOCX-019",
    question: "询价场景中，小额零星采购项目通过询价方式确认什么内容？",
  },
  {
    documentId: "DOCX-019",
    question:
      "当采购项目涉及仪器仪表类时，是否可以通过市场同类项目进行报价合理性分析？",
  },
  {
    documentId: "DOCX-020",
    question: "标前公示的截止时间应在工作日，且公示时间不少于多少日？",
  },
  {
    documentId: "DOCX-020",
    question: "采购方案提交院长办公会前需要完成哪些审查？",
  },
  {
    documentId: "DOCX-020",
    question:
      "如果标前公示时间不足3日，是否还能进行采购需求及方案的院长办公会决策？",
  },
  {
    documentId: "DOCX-021",
    question: "哪些类型的合作伙伴需要通过定向招募方式引入？",
  },
  {
    documentId: "DOCX-021",
    question: "哪些情况下合作伙伴会被列入负面行为清单或不良信用名单？",
  },
  {
    documentId: "DOCX-022",
    question: "采购项目依法招标的金额门槛如何规定？",
  },
  {
    documentId: "DOCX-022",
    question: "采购人如何处理非依法必须招标但预算金额超过10万元人民币的项目？",
  },
  {
    documentId: "DOCX-023",
    question: "法定代表人印章申请应遵循什么请示流程？",
  },
  {
    documentId: "DOCX-024",
    question: "哪些办公用品属于办公文具？",
  },
  {
    documentId: "DOCX-024",
    question: "哪些情况下可以采购电商平台或集采目录里没有的办公用品？",
  },
  {
    documentId: "DOCX-025",
    question: "哪些项目在年度投资计划下达前可以提前制定投资计划？",
  },
  {
    documentId: "DOCX-025",
    question: "投资计划调整需要经过哪些流程并如何上报？",
  },
  {
    documentId: "DOCX-025",
    question: "哪些情况下可以突破刚性管理项目集的投资额度限制？",
  },
  {
    documentId: "DOCX-026",
    question: "固定资产折旧的起算规则是什么？",
  },
  {
    documentId: "DOCX-026",
    question: "哪些部门需要共同参与固定资产减值准备的判断和计算？",
  },
  {
    documentId: "DOCX-026",
    question: "已提满折旧但仍可使用的固定资产是否可以被随意报废？",
  },
  {
    documentId: "DOCX-027",
    question:
      "研发活动直接消耗的材料、燃料和动力费用是否属于研究开发支出列支范围？",
  },
  {
    documentId: "DOCX-027",
    question: "研究开发支出中的人工成本如何归集？",
  },
  {
    documentId: "DOCX-027",
    question:
      "在何种情况下，研发项目的支出可以不纳入研究开发支出，而是从管理费用据实列支？",
  },
  {
    documentId: "DOCX-028",
    question: "项目启动阶段有哪些软件研发管理计划模板？",
  },
  {
    documentId: "DOCX-028",
    question:
      "准备与《1-项目启动-XXX项目软件研发管理计划20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-029",
    question:
      "准备与《1-项目启动-会议纪要模板20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-030",
    question:
      "是否有《2-需求阶段-XXX项目_VX.X.X_产品需求规格说明书模板20260612》这份模板可供参考？",
  },
  {
    documentId: "DOCX-030",
    question:
      "准备与《2-需求阶段-XXX项目_VX.X.X_产品需求规格说明书模板20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-031",
    question:
      "准备与《2-需求阶段-会议纪要模板20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-032",
    question:
      "是否有《2-需求阶段-需求变更评审会议纪要模板（模板）》这份模板可供参考？",
  },
  {
    documentId: "DOCX-033",
    question: "是否有《3-设计阶段-XXX系统设计说明书》这份模板可供参考？",
  },
  {
    documentId: "DOCX-033",
    question: "系统设计说明书有哪些参考模板？",
  },
  {
    documentId: "DOCX-034",
    question:
      "是否有《5-测试阶段-XXX项目_VX.X.X_测试报告模板20260612》这份模板可供参考？",
  },
  {
    documentId: "DOCX-035",
    question:
      "是否有《6-发布阶段-XXX项目_VX.X.X版本&迭代复盘报告20260612》这份模板可供参考？",
  },
  {
    documentId: "DOCX-035",
    question:
      "准备与《6-发布阶段-XXX项目_VX.X.X版本&迭代复盘报告20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-036",
    question:
      "是否有《6-发布阶段-XXX项目_VX.X.X验收测试报告20260612》这份模板可供参考？",
  },
  {
    documentId: "DOCX-036",
    question:
      "准备与《6-发布阶段-XXX项目_VX.X.X验收测试报告20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-037",
    question: "发布阶段可以查阅哪些版本发布方案模板？",
  },
  {
    documentId: "DOCX-037",
    question:
      "准备与《6-发布阶段-XXX项目版本发布方案模板20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-038",
    question:
      "是否有《6-发布阶段-XXX（系统平台）用户操作手册模板20260612》这份模板可供参考？",
  },
  {
    documentId: "DOCX-038",
    question:
      "准备与《6-发布阶段-XXX（系统平台）用户操作手册模板20260612》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-039",
    question:
      "是否有《6-发布阶段-版本发布评审会议纪要模板（参考）》这份模板可供参考？",
  },
  {
    documentId: "DOCX-039",
    question:
      "准备与《6-发布阶段-版本发布评审会议纪要模板（参考）》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-040",
    question:
      "是否有《6-发布阶段-迭代交付出口确认邮件模板（参考）》这份模板可供参考？",
  },
  {
    documentId: "DOCX-040",
    question:
      "准备与《6-发布阶段-迭代交付出口确认邮件模板（参考）》相关的材料时，应参考哪份模板？",
  },
  {
    documentId: "DOCX-041",
    question: "首单交付阶段的主要工作是什么？",
  },
  {
    documentId: "DOCX-041",
    question: "在规模化交付阶段，运维团队是否可以独立完成所有部署实施工作？",
  },
  {
    documentId: "DOCX-042",
    question: "产品经理在需求评审过程中需要记录哪些内容？",
  },
  {
    documentId: "DOCX-043",
    question: "产品开发模式是否允许在没有产品委员会评审的情况下启动？",
  },
  {
    documentId: "DOCX-044",
    question: "需求快验任务的核心目标是什么？",
  },
  {
    documentId: "DOCX-044",
    question: "哪些情况会导致需求快验任务不适用本指引？",
  },
  {
    documentId: "DOCX-045",
    question:
      "项目交付全流程工作规范中明确要求项目验收完成后是否进行版本迭代与功能升级？",
  },
  {
    documentId: "DOCX-045",
    question: "需求对齐评审通常由哪些角色参与？",
  },
  {
    documentId: "DOCX-045",
    question:
      "哪些类型的开发任务不适用于项目交付全流程工作规范，而是适用其他特定指引？",
  },
  {
    documentId: "DOCX-046",
    question: "业务团队收到设计文档后如何完成业务确认？",
  },
  {
    documentId: "DOCX-046",
    question:
      "开发中心在进行系统架构设计时，需要输出哪些文档并确保哪些人员参与评审？",
  },
  {
    documentId: "DOCX-046",
    question: "如果需求基线建立后需要变更，必须经过哪些流程才能调整基线内容？",
  },
  {
    documentId: "LEGACY-TEMPLATE",
    question:
      "是否有《6-发布阶段-XXX项目_VX.X.X版本发布报告20260612》这份模板可供参考？",
  },
  {
    documentId: "PRIOR-REAL-Q1",
    question:
      "员工申报的外部认证项目如果目录评审未通过，会如何处理？认证目录采用什么方式更新？",
  },
  {
    documentId: "PRIOR-REAL-Q3",
    question: "部门组织绩效管理中，年度考核和预考核分别指什么？",
  },
  {
    documentId: "PRIOR-REAL-Q4",
    question:
      "申请公司公章要从哪个系统入口发起，需要填写或上传什么，完整审批流程是什么？",
  },
  {
    documentId: "PRIOR-REAL-Q6",
    question: "产品开发全流程工作规范适用于哪些研发活动，覆盖哪些主要角色？",
  },
  {
    documentId: "PRIOR-REAL-Q7",
    question:
      "采用公开的招标、询比或竞价方式时，至少需要多少家符合资格条件的供应商参与？",
  },
];
