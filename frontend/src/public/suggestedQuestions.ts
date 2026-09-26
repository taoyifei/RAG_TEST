// 推荐题面仅来自已审核的公共推荐接口；这里保存展示所需的类型。
export type SuggestedQuestionStyle = "SHORT" | "STANDARD" | "COMPOUND";

export interface SuggestedQuestion {
  id: string;
  question: string;
  documentId: string;
  style: SuggestedQuestionStyle;
  topicKey: string;
  enabled: boolean;
}
