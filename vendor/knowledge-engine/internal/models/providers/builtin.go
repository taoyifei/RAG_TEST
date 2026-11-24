package providers

import "industrialagent.local/knowledge-engine/internal/models/internal/configcopy"

// Builtins returns independent provider definitions without registering global state.
func Builtins() []*Definition {
	return configcopy.Clone([]*Definition{
		newAliyunProvider(),
		newAnthropicProvider(),
		newAzureOpenaiProvider(),
		newCohereProvider(),
		newDeepseekProvider(),
		newGeminiProvider(),
		newGenericProvider(),
		newGpustackProvider(),
		newHuggingFaceTEIProvider(),
		newHunyuanProvider(),
		newJinaProvider(),
		newLitellmProvider(),
		newLkeapProvider(),
		newLongcatProvider(),
		newMimoProvider(),
		newMinimaxProvider(),
		newModelscopeProvider(),
		newMoonshotProvider(),
		newNovitaProvider(),
		newNvidiaProvider(),
		newOpenaiProvider(),
		newOpenrouterProvider(),
		newPineconeProvider(),
		newQianfanProvider(),
		newQiniuProvider(),
		newRequestyProvider(),
		newSiliconflowProvider(),
		newVolcengineProvider(),
		newKnowledgeEngineCloudProvider(),
		newZhipuProvider(),
	})
}
