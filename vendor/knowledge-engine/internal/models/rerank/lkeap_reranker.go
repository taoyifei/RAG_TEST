package rerank

import (
	"strings"

	"industrialagent.local/knowledge-engine/internal/models/api"
	"industrialagent.local/knowledge-engine/internal/models/api/tencentlkeap"
	modelruntime "industrialagent.local/knowledge-engine/internal/models/runtime"
)

// Tencent action defaults retained for existing callers.
const (
	LKEAPRerankEndpoint     = tencentlkeap.LKEAPRerankEndpoint
	LKEAPDefaultRegion      = tencentlkeap.LKEAPDefaultRegion
	LKEAPDefaultRerankModel = tencentlkeap.LKEAPDefaultRerankModel
)

// Test composition may route the action SDK to a local stand-in.
var lkeapEndpoint, lkeapScheme = LKEAPRerankEndpoint, "HTTPS"

func newLKEAPClient(config *RerankerConfig, resolved *modelruntime.Resolved) (api.Reranker, error) {
	secret := strings.TrimSpace(config.AppSecret)
	if secret == "" {
		secret = config.ExtraConfig["secret_key"]
	}
	return tencentlkeap.New(
		tencentlkeap.Config{
			SecretID:  config.APIKey,
			SecretKey: secret,
			Region:    config.ExtraConfig["region"],
			Model:     resolved.RemoteModel,
			Endpoint:  lkeapEndpoint,
			Scheme:    lkeapScheme,
		},
	)
}
