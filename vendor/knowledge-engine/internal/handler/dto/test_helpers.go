package dto

import (
	"context"

	"industrialagent.local/knowledge-engine/internal/types"
)

func adminContext() context.Context {
	ctx := context.Background()
	return context.WithValue(ctx, types.TenantRoleContextKey, types.TenantRoleAdmin)
}

func viewerContext() context.Context {
	ctx := context.Background()
	return context.WithValue(ctx, types.TenantRoleContextKey, types.TenantRoleViewer)
}

func ownerContext() context.Context {
	ctx := context.Background()
	return context.WithValue(ctx, types.TenantRoleContextKey, types.TenantRoleOwner)
}
