package handler

import (
	"context"

	"industrialagent.local/knowledge-engine/internal/config"
	"industrialagent.local/knowledge-engine/internal/types/interfaces"
)

const (
	tenantSelfServiceCreationSettingKey = "tenant.self_service_creation_enabled"
	tenantSelfServiceCreationEnvName    = "KNOWLEDGE_ENGINE_TENANT_SELF_SERVICE_CREATION_ENABLED"
)

// resolveTenantSelfServiceCreationEnabled is the shared policy resolver used
// both by POST /tenants enforcement and /auth/me capability projection. Keeping
// both on one function prevents the UI from advertising a capability that the
// backend would reject.
func resolveTenantSelfServiceCreationEnabled(
	ctx context.Context,
	cfg *config.Config,
	settings interfaces.SystemSettingService,
) bool {
	enabled := true
	if cfg != nil && cfg.Tenant != nil {
		enabled = cfg.Tenant.IsSelfServiceCreationEnabled()
	}
	if settings == nil {
		return enabled
	}
	return settings.GetBool(
		ctx,
		tenantSelfServiceCreationSettingKey,
		tenantSelfServiceCreationEnvName,
		enabled,
	)
}
