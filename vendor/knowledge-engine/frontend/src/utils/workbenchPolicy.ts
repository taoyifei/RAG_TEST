import type { AutomationInput } from '@/api/automation'

/** 工作助手的任务每次使用新会话，只读当前工作区，不继承前一次结果。 */
export function workbenchAutomationInput(input: AutomationInput): AutomationInput {
  return {
    ...input,
    model_id: '',
    access_level: 'read_only',
    session_mode: 'per_run',
    carry_last_result: false,
  }
}

/** 通知只导航到独立工作区已有的任务或会话页。 */
export function workbenchNotificationLocation(link: string): string {
  return /^\/platform\/(?:automations|chat)\/[A-Za-z0-9_-]+(?:\?run=[A-Za-z0-9_-]+)?$/.test(link) ? link : ''
}
