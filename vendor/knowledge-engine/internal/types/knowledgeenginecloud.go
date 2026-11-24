package types

// KnowledgeEngineCloudStatusResult 状态检查结果
type KnowledgeEngineCloudStatusResult struct {
	HasModels   bool   `json:"has_models"`       // 是否已配置 KnowledgeEngineCloud 凭证
	NeedsReinit bool   `json:"needs_reinit"`     // 是否需要重新初始化（凭证损坏）
	Reason      string `json:"reason,omitempty"` // 需要重新初始化的原因
}
