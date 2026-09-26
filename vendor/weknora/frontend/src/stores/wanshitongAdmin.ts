import { ref } from 'vue'
import { defineStore } from 'pinia'

/** 问题榜到推荐题编辑器的临时草稿，不写 URL 或浏览器存储。 */
export const useWanshitongAdminStore = defineStore('wanshitongAdmin', () => {
  const seedQuestion = ref('')
  function startRecommendationDraft(question: string) {
    seedQuestion.value = question
  }
  function consumeRecommendationDraft(): string {
    const question = seedQuestion.value
    seedQuestion.value = ''
    return question
  }
  return { seedQuestion, startRecommendationDraft, consumeRecommendationDraft }
})
