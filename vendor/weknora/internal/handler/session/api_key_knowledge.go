package session

import (
	"context"

	"github.com/Tencent/WeKnora/internal/errors"
	"github.com/Tencent/WeKnora/internal/types"
)

type knowledgeTargetAuthorizer interface {
	AuthorizeKnowledgeTargets(context.Context, []string, []string) (context.Context, error)
}

func (h *Handler) authorizeKnowledgeTargets(
	ctx context.Context, kbIDs, knowledgeIDs []string,
) (context.Context, error) {
	if scope, ok := types.TenantAPIKeyScopeFromContext(ctx); ok &&
		scope.IsKnowledgeBaseRestricted() && len(knowledgeIDs) > 0 {
		authorizer, available := h.sessionService.(knowledgeTargetAuthorizer)
		if !available {
			return ctx, errors.NewForbiddenError("knowledge scope verification is unavailable")
		}
		return authorizer.AuthorizeKnowledgeTargets(ctx, kbIDs, knowledgeIDs)
	}
	return ctx, types.AuthorizeTenantAPIKeyKnowledgeTargets(ctx, kbIDs, knowledgeIDs)
}
