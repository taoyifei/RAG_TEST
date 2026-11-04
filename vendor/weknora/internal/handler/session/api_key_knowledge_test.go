package session

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Tencent/WeKnora/internal/middleware"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/Tencent/WeKnora/internal/types/interfaces"
	"github.com/gin-gonic/gin"
)

type keyFileSessionStub struct {
	interfaces.SessionService
	searches int
}

func (s *keyFileSessionStub) GetOwnedSession(context.Context, string) (*types.Session, error) {
	return &types.Session{ID: "session-1", TenantID: 9}, nil
}

func (s *keyFileSessionStub) AuthorizeKnowledgeTargets(ctx context.Context, kbIDs, ids []string) (context.Context, error) {
	return types.VerifyTenantAPIKeyKnowledgeTargets(ctx, kbIDs, ids,
		func(_ context.Context, _ uint64, ids []string) ([]*types.Knowledge, error) {
			rows := []*types.Knowledge{}
			for _, id := range ids {
				switch id {
				case "allowed":
					rows = append(rows, &types.Knowledge{ID: id, TenantID: 9, KnowledgeBaseID: "kb-allowed"})
				case "blocked":
					rows = append(rows, &types.Knowledge{ID: id, TenantID: 9, KnowledgeBaseID: "kb-blocked"})
				case "foreign":
					rows = append(rows, &types.Knowledge{ID: id, TenantID: 10, KnowledgeBaseID: "kb-allowed"})
				}
			}
			return rows, nil
		})
}

func (s *keyFileSessionStub) SearchKnowledge(
	ctx context.Context, kbIDs, ids []string, _ []types.TagScope, _ string, _ *types.KnowledgeSearchOptions,
) (*types.RetrievalResult, error) {
	if err := types.AuthorizeTenantAPIKeyKnowledgeTargets(ctx, kbIDs, ids); err != nil {
		return nil, err
	}
	s.searches++
	return &types.RetrievalResult{Results: []*types.SearchResult{}}, nil
}

func TestRestrictedAPIKeyFileTargetsPassServerVerificationBeforeQAAndSearch(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cases := []struct {
		name string
		ids  []string
		want int
	}{
		{"allowed", []string{"allowed"}, http.StatusOK},
		{"blocked KB", []string{"blocked"}, http.StatusForbidden},
		{"foreign tenant", []string{"foreign"}, http.StatusForbidden},
		{"unknown", []string{"missing"}, http.StatusForbidden},
		{"mixed", []string{"allowed", "blocked"}, http.StatusForbidden},
	}
	for _, route := range []string{"qa/session-1", "search"} {
		for _, tc := range cases {
			t.Run(route+"/"+tc.name, func(t *testing.T) {
				sessions := &keyFileSessionStub{}
				h := &Handler{sessionService: sessions}
				router := gin.New()
				router.Use(middleware.ErrorHandler())
				router.Use(func(c *gin.Context) {
					ctx := context.WithValue(c.Request.Context(), types.TenantIDContextKey, uint64(9))
					ctx = types.WithCaller(ctx, types.Caller{TenantID: 9, UserID: "reader"})
					ctx = types.WithTenantAPIKeyScope(ctx, types.TenantAPIKeyScope{
						KeyID: 7, KnowledgeBaseIDs: types.StringArray{"kb-allowed"},
						Capabilities: types.StringArray{"chat", "retrieve"},
					})
					c.Request = c.Request.WithContext(ctx)
					c.Next()
				})
				router.POST("/qa/:session_id", func(c *gin.Context) {
					req, _, err := h.parseQARequest(c, "scope-test")
					if err != nil {
						_ = c.Error(err)
						return
					}
					if err := types.AuthorizeTenantAPIKeyKnowledgeTargets(req.ctx, nil, req.knowledgeIDs); err != nil {
						_ = c.Error(err)
						return
					}
					c.JSON(http.StatusOK, gin.H{"ok": true})
				})
				router.POST("/search", h.SearchKnowledge)
				body, _ := json.Marshal(map[string]interface{}{
					"query": "only selected files", "knowledge_ids": tc.ids,
				})
				request := httptest.NewRequest(http.MethodPost, "/"+route, bytes.NewReader(body))
				request.Header.Set("Content-Type", "application/json")
				response := httptest.NewRecorder()
				router.ServeHTTP(response, request)
				if response.Code != tc.want {
					t.Fatalf("status=%d want=%d body=%s", response.Code, tc.want, response.Body.String())
				}
				if tc.want == http.StatusForbidden && sessions.searches != 0 {
					t.Fatal("unauthorized files reached retrieval")
				}
			})
		}
	}
}
