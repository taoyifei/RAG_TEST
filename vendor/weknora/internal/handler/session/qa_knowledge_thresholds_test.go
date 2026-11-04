package session

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Tencent/WeKnora/internal/agent"
	"github.com/Tencent/WeKnora/internal/agent/hitl"
	"github.com/Tencent/WeKnora/internal/middleware"
	"github.com/Tencent/WeKnora/internal/stream"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/gin-gonic/gin"
)

func TestKnowledgeThresholdRequestModeReachesAsyncQAAndDoesNotPersistInSession(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := &Handler{sessionService: &keyFileSessionStub{}}
	router := gin.New()
	router.Use(middleware.ErrorHandler())
	var seen []bool
	router.POST("/qa/:session_id", func(c *gin.Context) {
		request, _, err := h.parseQARequest(c, "threshold-test")
		if err != nil {
			_ = c.Error(err)
			return
		}
		mode := request.buildQARequest().KnowledgeIDsUseKBThresholds
		if request.assistantMessage.ExecutionContext.KnowledgeIDsUseKBThresholds != mode {
			t.Fatal("message snapshot disagrees with async QA request")
		}
		seen = append(seen, mode)
		c.JSON(http.StatusOK, gin.H{"mode": mode})
	})
	bodies := []string{
		`{"query":"Q","knowledge_ids":["allowed"]}`,
		`{"query":"Q","knowledge_ids":["allowed"],"knowledge_ids_use_kb_thresholds":true}`,
		`{"query":"Q","knowledge_ids":["allowed"],"knowledge_ids_use_kb_thresholds":false}`,
		`{"query":"Q","knowledge_ids":["allowed"]}`,
	}
	want := []bool{false, true, false, false}
	for index, body := range bodies {
		request := httptest.NewRequest(http.MethodPost, "/qa/session-1", bytes.NewBufferString(body))
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		if response.Code != http.StatusOK {
			t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
		}
		if seen[index] != want[index] {
			t.Fatalf("request %d mode=%v want=%v", index, seen[index], want[index])
		}
	}
}

func TestResumeKeepsKnowledgeThresholdModeOfOriginalRequest(t *testing.T) {
	for _, useKBThresholds := range []bool{false, true} {
		c, ctx := newResolveAgentTestContext(1)
		messages := &resumeMessages{rows: map[string]*types.Message{
			"m": {ID: "m", SessionID: "s", RunState: types.MessageRunStateAwaitingInput},
		}}
		broker := hitl.NewBroker(nil, nil)
		request, err := json.Marshal(types.ResumableAgentRequest{
			AgentID: "own-agent", KnowledgeIDs: []string{"doc-1"}, KnowledgeIDsUseKBThresholds: useKBThresholds,
		})
		if err != nil {
			t.Fatal(err)
		}
		snapshot, err := json.Marshal(agent.RunSnapshot{Version: agent.RunSnapshotVersion, Request: request})
		if err != nil {
			t.Fatal(err)
		}
		if err := broker.Suspensions().Save(ctx, &types.AgentRunSuspension{
			AssistantMessageID: "m", PromptID: "p", Snapshot: types.JSON(snapshot),
		}); err != nil {
			t.Fatal(err)
		}
		h := &Handler{
			promptBroker: broker, messageService: messages, sessionService: &resumeSessionService{},
			streamManager: stream.NewMemoryStreamManager(),
			customAgentService: &resolveOwnAgentStub{agent: &types.CustomAgent{
				ID: "own-agent", TenantID: 1,
			}},
		}
		restored, _, err := h.buildResumeRequest(ctx, c, &types.AgentPrompt{
			ID: "p", SessionID: "s", AssistantMessageID: "m", Kind: types.AgentPromptKindToolApproval,
		})
		if err != nil {
			t.Fatal(err)
		}
		if restored.buildQARequest().KnowledgeIDsUseKBThresholds != useKBThresholds {
			t.Fatalf("resume lost original threshold mode %v", useKBThresholds)
		}
		// 新请求上下文不读取上次恢复状态或会话默认值。
		next := &qaRequestContext{ctx: context.Background(), session: restored.session, assistantMessage: &types.Message{ID: "next"}}
		if next.buildQARequest().KnowledgeIDsUseKBThresholds {
			t.Fatal("resumed mode polluted next request")
		}
	}
}
