import { toRaw } from 'vue'

/** 将响应式设置复制为独立的数据快照，供保存和回读表单使用。 */
export function clonePublicSettings<T extends object>(settings: T): T {
  return structuredClone(toRaw(settings))
}
