export const zhCN = {
  brand: "企业知识助手",
  productCaption: "知识库工作台",
  navigation: {
    workspace: "工作台",
    knowledge: "知识库",
    documents: "文档管理",
    chat: "问答",
    modelServices: "模型服务",
    jobs: "处理任务",
    revisions: "索引记录",
    retrieval: "检索调试",
    system: "系统状态",
    access: "接口访问",
    operations: "运维工具",
  },
  status: {
    succeeded: "已完成",
    failed: "失败",
    active: "使用中",
    inactive: "未使用",
    pending: "等待中",
    running: "处理中",
    configured: "已配置",
    validated: "已验证",
    degraded: "需要检查",
    not_verified: "未验证",
    mock_validated: "离线验证通过",
    live_validated: "在线验证通过",
    true: "是",
    false: "否",
  } satisfies Record<string, string>,
  impact: {
    NO_REINDEX: "无需重建索引",
    SERVING_RELOAD: "重新加载查询配置",
    NEW_INDEX_REVISION_REQUIRED: "需要构建新索引版本",
  } satisfies Record<string, string>,
} as const;

export function localizeStatus(value: string | boolean): string {
  const key = String(value);
  return key in zhCN.status
    ? zhCN.status[key as keyof typeof zhCN.status]
    : key;
}
