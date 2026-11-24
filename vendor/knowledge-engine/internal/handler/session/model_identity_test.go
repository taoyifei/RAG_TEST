package session

import (
	"testing"

	"github.com/stretchr/testify/require"
	"industrialagent.local/knowledge-engine/internal/stream"
	"industrialagent.local/knowledge-engine/internal/types"
)

func TestSetupSSEStreamPropagatesBusinessSession(t *testing.T) {
	for _, mode := range []qaMode{qaModeNormal, qaModeAgent} {
		h := &Handler{streamManager: stream.NewMemoryStreamManager()}
		reqCtx := &qaRequestContext{
			c:                newSteerWiringGinContext(),
			ctx:              types.WithSessionID(t.Context(), "parent-session"),
			sessionID:        "business-session",
			session:          &types.Session{ID: "business-session"},
			assistantMessage: &types.Message{ID: "assistant", SessionID: "business-session"},
		}
		streamCtx := h.setupSSEStream(reqCtx, false, mode)
		require.NotNil(t, streamCtx)
		id, ok := types.SessionIDFromContext(streamCtx.asyncCtx)
		require.True(t, ok)
		require.Equal(t, reqCtx.session.ID, id)
		streamCtx.cancel()
	}
}
