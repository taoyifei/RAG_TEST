package service

import (
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"industrialagent.local/knowledge-engine/internal/types"
)

func TestResetKnowledgeForReparseClearsPreviousAttemptState(t *testing.T) {
	processedAt := time.Now()
	knowledge := &types.Knowledge{
		ParseStatus:          types.ParseStatusFailed,
		EnableStatus:         "enabled",
		Description:          "previous description",
		ProcessedAt:          &processedAt,
		ErrorMessage:         "embedding request failed: context deadline exceeded",
		EmbeddingModelID:     "old-model",
		PendingSubtasksCount: 3,
	}
	kb := &types.KnowledgeBase{EmbeddingModelID: "new-model"}

	resetKnowledgeForReparse(knowledge, kb)

	require.Equal(t, types.ParseStatusPending, knowledge.ParseStatus)
	require.Equal(t, "disabled", knowledge.EnableStatus)
	require.Empty(t, knowledge.Description)
	require.Nil(t, knowledge.ProcessedAt)
	require.Empty(t, knowledge.ErrorMessage)
	require.Equal(t, "new-model", knowledge.EmbeddingModelID)
	require.Zero(t, knowledge.PendingSubtasksCount)
}
