App({
  onLaunch() {
    const settings = wx.getStorageSync("knowledgeengine_settings");
    if (!settings) {
      wx.setStorageSync("knowledgeengine_settings", {
        baseUrl: "http://localhost:8080",
        apiKey: "",
        selectedKnowledgeBaseId: "",
        locale: "zh"
      });
    }
  }
});
