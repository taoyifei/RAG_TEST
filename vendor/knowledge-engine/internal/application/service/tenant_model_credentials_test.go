package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
	"industrialagent.local/knowledge-engine/internal/models/providers"
	"industrialagent.local/knowledge-engine/internal/types"
	"industrialagent.local/knowledge-engine/internal/types/interfaces"
)

type modelCredentialTenants struct {
	interfaces.TenantRepository
	owner *types.Tenant
	calls []uint64
}

func (r *modelCredentialTenants) GetTenantByID(_ context.Context, id uint64) (*types.Tenant, error) {
	r.calls = append(r.calls, id)
	return r.owner, nil
}

func TestSharedChatModelCredentialsUseModelTenant(t *testing.T) {
	owner := &types.Tenant{ID: 1, Credentials: &types.CredentialsConfig{
		KnowledgeEngineCloud: &types.KnowledgeEngineCloudCredentials{AppID: "owner-app", AppSecret: "owner-secret"},
	}}
	caller := &types.Tenant{ID: 2, Credentials: &types.CredentialsConfig{
		KnowledgeEngineCloud: &types.KnowledgeEngineCloudCredentials{AppID: "caller-app", AppSecret: "caller-secret"},
	}}
	ctx := context.WithValue(context.Background(), types.TenantIDContextKey, uint64(2))
	ctx = context.WithValue(ctx, types.TenantInfoContextKey, caller)
	repo := &modelCredentialTenants{owner: owner}
	svc := &modelService{tenantService: &tenantService{repo: repo}}
	params := &types.ModelParameters{Provider: providers.KnowledgeEngineCloudID}

	appID, secret := svc.resolveKnowledgeEngineCloudCredentials(ctx, params)
	require.Equal(t, "caller-app", appID)
	require.Equal(t, "caller-secret", secret)
	require.Empty(t, repo.calls, "same-tenant credentials use middleware cache")

	cm := &types.ChatManage{PipelineRequest: types.PipelineRequest{ChatModelTenantID: 1}}
	appID, secret = svc.resolveKnowledgeEngineCloudCredentials(cm.ChatModelContext(ctx), params)
	require.Equal(t, "owner-app", appID)
	require.Equal(t, "owner-secret", secret)
	require.Equal(t, []uint64{1}, repo.calls)
	require.Equal(t, uint64(2), types.MustTenantIDFromContext(ctx))
}
