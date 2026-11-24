package access

import (
	"context"

	"industrialagent.local/knowledge-engine/internal/config"
	"industrialagent.local/knowledge-engine/internal/types"
)

// IsCrossTenantSuperuser reports whether the caller's user attribute and the
// cluster-wide flag both allow bypassing workspace role gates. API-key and
// resource-scope authorization remain separate checks.
func IsCrossTenantSuperuser(ctx context.Context, cfg *config.Config) bool {
	if cfg == nil || cfg.Tenant == nil || !cfg.Tenant.EnableCrossTenantAccess {
		return false
	}
	u, ok := ctx.Value(types.UserContextKey).(*types.User)
	return ok && u != nil && u.CanAccessAllTenants
}
