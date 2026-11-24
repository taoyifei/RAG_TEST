package router

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"industrialagent.local/knowledge-engine/internal/handler"
)

// RegisterGraphSearchRoutes is a read-only KB content route, even though the
// query is POSTed. Role, API-key capability and exact KB grant are independent.
func RegisterGraphSearchRoutes(r *gin.RouterGroup, h *handler.GraphSearchHandler, g *rbacGuards) {
	if h == nil {
		return
	}
	g.apiKeyRoute(r, http.MethodPost, "/knowledge-bases/:id/graph-search",
		apiKeyRetrieve(apiKeyFullAccess()), g.Viewer(), g.KBAccessRead("id"), h.Search)
}
