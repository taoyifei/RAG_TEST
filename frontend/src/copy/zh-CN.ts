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
    configured: "已保存",
    validated: "已验证",
    degraded: "需要检查",
    healthy: "运行正常",
    unhealthy: "运行异常",
    INDEX_CORRUPT: "索引需要修复",
    就绪: "就绪",
    not_verified: "尚未验证",
    mock_validated: "离线模拟通过",
    live_validated: "接口测试通过",
    configuration_incomplete: "本地配置待完善",
    needs_retest: "配置已变化需重新测试",
    ANSWERABLE: "有参考原文",
    UNANSWERABLE: "暂无足够依据",
    revoked: "已吊销",
    queued: "等待中",
    failed_retryable: "等待重试",
    failed_terminal: "处理失败",
    cancelled: "已取消",
    draft: "草稿",
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
    : "未知状态（技术详情可查看原值）";
}

export function operationLabel(operation: string): string {
  return (
    (
      {
        "embedding.document": "文档向量",
        "embedding.query": "查询向量",
        reranking: "结果重排",
      } as Record<string, string>
    )[operation] ?? "未知能力"
  );
}

export function validationMessage(item: {
  status: string;
  http_category: string;
  request_dispatched?: boolean | null;
  validation_mode?: string;
}): string {
  if (item.http_category.includes("budget"))
    return "测试预算已用尽，未继续发送请求。";
  if (item.request_dispatched === false)
    return "配置尚未完整，请检查连接设置。本次未发送请求。";
  if (["http_401", "http_403"].includes(item.http_category))
    return "接口拒绝了访问，请核对密钥权限、地域和工作空间。";
  if (item.http_category === "http_429") return "服务暂时限流，请稍后重试。";
  if (item.status === "succeeded")
    return item.validation_mode === "mock" || item.http_category === "mock_200"
      ? "离线模拟通过"
      : "接口测试通过";
  if (
    [
      "response_invalid",
      "invalid_response",
      "parse_error",
      "bad_json",
      "invalid_contract",
    ].includes(item.http_category)
  )
    return "已收到响应，但返回格式与当前适配器不一致。";
  return "测试未通过，请查看技术详情或编辑连接后重试。";
}
