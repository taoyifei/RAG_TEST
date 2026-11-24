const { getSettings, saveSettings } = require("./config");

const MESSAGES = {
  zh: {
    tabKnowledge: "知识库",
    tabChat: "问答",
    tabSettings: "设置",
    navKnowledge: "知识库",
    navChat: "问答",
    navSettings: "设置",
    knowledgeTitle: "KnowledgeEngine 知识库",
    knowledgeSubtitle: "在微信中选择知识库，导入网页或提问。",
    needsSettings: "请先配置 KnowledgeEngine API 地址和 API Key，再加载知识库。",
    openSettings: "打开设置",
    knowledgeBase: "知识库",
    tapToSelect: "点击选择",
    refreshKnowledgeBases: "刷新知识库列表",
    urlLabel: "网页链接",
    urlPlaceholder: "https://industrial-agent.example/source",
    importUrl: "导入链接",
    loadedKnowledgeBases: "已加载 {count} 个知识库。",
    noKnowledgeBases: "未找到知识库。",
    loadFailed: "加载失败",
    imported: "导入成功",
    importFailed: "导入失败",
    chatTitle: "知识问答",
    chatSubtitle: "向当前选中的 KnowledgeEngine 知识库提问。",
    question: "问题",
    questionPlaceholder: "输入你的问题…",
    askKnowledgeEngine: "向 KnowledgeEngine 提问",
    answer: "回答",
    chatFailed: "问答失败",
    sessionIdMissing: "会话接口未返回 session id。",
    settingsTitle: "连接 KnowledgeEngine",
    settingsSubtitle: "填写 KnowledgeEngine API 地址与租户 API Key。",
    apiBaseUrl: "API 地址",
    apiBaseUrlPlaceholder: "https://your-knowledgeengine.example.com",
    apiKey: "API Key",
    apiKeyPlaceholder: "sk-...",
    language: "界面语言",
    languageZh: "中文",
    languageEn: "English",
    saveSettings: "保存设置",
    saved: "已保存",
    missingBaseUrl: "请先配置 KnowledgeEngine API 地址。",
    missingApiKey: "请先配置 KnowledgeEngine API Key。"
  },
  en: {
    tabKnowledge: "Knowledge",
    tabChat: "Chat",
    tabSettings: "Settings",
    navKnowledge: "Knowledge",
    navChat: "Chat",
    navSettings: "Settings",
    knowledgeTitle: "KnowledgeEngine Knowledge",
    knowledgeSubtitle: "Import a web page into a selected knowledge base from WeChat.",
    needsSettings: "Configure the KnowledgeEngine API base URL and API key before loading knowledge bases.",
    openSettings: "Open Settings",
    knowledgeBase: "Knowledge Base",
    tapToSelect: "Tap to select",
    refreshKnowledgeBases: "Refresh knowledge bases",
    urlLabel: "URL",
    urlPlaceholder: "https://industrial-agent.example/source",
    importUrl: "Import URL",
    loadedKnowledgeBases: "Loaded {count} knowledge bases.",
    noKnowledgeBases: "No knowledge bases found.",
    loadFailed: "Load failed",
    imported: "Imported",
    importFailed: "Import failed",
    chatTitle: "Knowledge Chat",
    chatSubtitle: "Ask the selected KnowledgeEngine knowledge base.",
    question: "Question",
    questionPlaceholder: "Ask something...",
    askKnowledgeEngine: "Ask KnowledgeEngine",
    answer: "Answer",
    chatFailed: "Chat failed",
    sessionIdMissing: "The session API did not return a session id.",
    settingsTitle: "Connect KnowledgeEngine",
    settingsSubtitle: "Use your KnowledgeEngine API endpoint and tenant API key.",
    apiBaseUrl: "API Base URL",
    apiBaseUrlPlaceholder: "https://your-knowledgeengine.example.com",
    apiKey: "API Key",
    apiKeyPlaceholder: "sk-...",
    language: "Language",
    languageZh: "中文",
    languageEn: "English",
    saveSettings: "Save settings",
    saved: "Saved",
    missingBaseUrl: "Please configure the KnowledgeEngine API base URL first.",
    missingApiKey: "Please configure the KnowledgeEngine API key first."
  }
};

function normalizeLocale(locale) {
  return locale === "en" ? "en" : "zh";
}

function getLocale() {
  return normalizeLocale(getSettings().locale);
}

function setLocale(locale) {
  const next = normalizeLocale(locale);
  saveSettings({ locale: next });
  return next;
}

function t(key, vars = {}, locale = getLocale()) {
  const table = MESSAGES[normalizeLocale(locale)] || MESSAGES.zh;
  let text = table[key] || MESSAGES.en[key] || key;
  Object.keys(vars).forEach((name) => {
    text = text.replace(new RegExp(`\\{${name}\\}`, "g"), String(vars[name]));
  });
  return text;
}

function getMessages(locale = getLocale()) {
  return MESSAGES[normalizeLocale(locale)] || MESSAGES.zh;
}

function applyTabBar(locale = getLocale()) {
  const lang = normalizeLocale(locale);
  const items = [
    {
      index: 0,
      text: t("tabKnowledge", {}, lang),
      iconPath: "assets/tab/knowledge.png",
      selectedIconPath: "assets/tab/knowledge-active.png"
    },
    {
      index: 1,
      text: t("tabChat", {}, lang),
      iconPath: "assets/tab/chat.png",
      selectedIconPath: "assets/tab/chat-active.png"
    },
    {
      index: 2,
      text: t("tabSettings", {}, lang),
      iconPath: "assets/tab/settings.png",
      selectedIconPath: "assets/tab/settings-active.png"
    }
  ];
  items.forEach((item) => {
    try {
      wx.setTabBarItem(item);
    } catch (error) {
      // ignore when called outside tab page context
    }
  });
}

function applyNavTitle(key, locale = getLocale()) {
  try {
    wx.setNavigationBarTitle({ title: t(key, {}, locale) });
  } catch (error) {
    // ignore in non-page contexts
  }
}

module.exports = {
  MESSAGES,
  applyNavTitle,
  applyTabBar,
  getLocale,
  getMessages,
  normalizeLocale,
  setLocale,
  t
};
