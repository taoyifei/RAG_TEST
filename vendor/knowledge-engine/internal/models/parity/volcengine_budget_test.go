package parity

import (
	"testing"

	"github.com/stretchr/testify/require"
	"industrialagent.local/knowledge-engine/internal/models/api"
	"industrialagent.local/knowledge-engine/internal/models/chat"
	modelruntime "industrialagent.local/knowledge-engine/internal/models/runtime"
	"industrialagent.local/knowledge-engine/internal/types"
)

func TestVolcengineCompletionBudgetFieldsAreMutuallyExclusive(t *testing.T) {
	vendor, ok := modelruntime.Get("volcengine")
	require.True(t, ok)
	for _, model := range vendor.ModelsByType(types.ModelTypeKnowledgeQA) {
		t.Run(model.ID, func(t *testing.T) {
			for _, stream := range []bool{false, true} {
				for _, thinking := range []bool{false, true} {
					for _, budget := range []int{0, 256} {
						body := buildBody(t, &chat.ChatConfig{
							Provider: "volcengine", ModelName: model.ID, BaseURL: "http://127.0.0.1:9/v3",
							Spec: &types.ModelSpecOverride{Compat: map[string]any{
								"extra_body": map[string]any{"max_tokens": 1024, "max_completion_tokens": 2048},
							}},
						}, &api.Options{MaxTokens: budget, Thinking: ptrBool(thinking)}, stream)
						require.NotContains(t, body, "max_tokens")
						want := 2048
						if budget > 0 {
							want = budget
						}
						require.Equal(t, float64(want), body["max_completion_tokens"])
					}
				}
			}
		})
	}
}
