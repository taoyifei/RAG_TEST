import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN.ts'
import ruRU from './locales/ru-RU.ts'
import enUS from './locales/en-US.ts'
import koKR from './locales/ko-KR.ts'
import jaJP from './locales/ja-JP.ts'
import { BUILT_IN_DEFAULT, resolveDefaultLocale } from './resolveDefaultLocale.ts'
import { WANSHITONG_GATEWAY_AUTH } from '@/config/wanshitongGateway'

const nativeMessages = {
  'zh-CN': zhCN,
  'en-US': enUS,
  'ru-RU': ruRU,
  'ko-KR': koKR,
  'ja-JP': jaJP
}

/** 只替换面向用户的产品文案，不改引擎、云模型、接口与诊断中的专有名称。 */
function brandMessages<T extends Record<string, unknown>>(native: T): T {
  const branded = structuredClone(native)
  type ProductCopy = {
    createChat: { title: string }
    newUserGuide: { steps: { knowledge: { desc: string }; welcome: { title: string } } }
  }
  for (const language of Object.keys(branded)) {
    const catalog = branded[language] as ProductCopy
    catalog.createChat.title = catalog.createChat.title.replace('WeKnora', '湾事通')
    catalog.newUserGuide.steps.knowledge.desc =
      catalog.newUserGuide.steps.knowledge.desc.replace('WeKnora', '湾事通')
    catalog.newUserGuide.steps.welcome.title =
      catalog.newUserGuide.steps.welcome.title.replace('WeKnora', '湾事通')
  }
  return branded
}

const messages = WANSHITONG_GATEWAY_AUTH ? brandMessages(nativeMessages) : nativeMessages

// User's explicit past choice wins; otherwise use the deployment default.
const savedLocale = localStorage.getItem('locale') || resolveDefaultLocale(
  window.__RUNTIME_CONFIG__?.DEFAULT_LOCALE,
  import.meta.env.VITE_DEFAULT_LOCALE,
)

const i18n = createI18n({
  legacy: false,
  locale: savedLocale,
  fallbackLocale: BUILT_IN_DEFAULT,
  globalInjection: true,
  // Some translations intentionally embed `<strong>` markup (e.g. agent step summaries).
  // We render them via v-html with our own sanitization, so silence vue-i18n's HTML warning
  // to avoid flooding the console and slowing renders during history loads.
  warnHtmlMessage: false,
  messages
})

export default i18n
