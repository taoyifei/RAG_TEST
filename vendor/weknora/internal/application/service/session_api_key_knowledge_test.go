package service

import (
	"context"
	"testing"

	"github.com/Tencent/WeKnora/internal/logger"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/Tencent/WeKnora/internal/types/interfaces"
)

type keyKnowledgeLookup struct {
	interfaces.KnowledgeService
	rows  []*types.Knowledge
	calls int
}

func (k *keyKnowledgeLookup) GetKnowledgeBatch(_ context.Context, tenantID uint64, ids []string) ([]*types.Knowledge, error) {
	k.calls++
	if tenantID != 9 {
		panic("lookup must use authenticated caller tenant")
	}
	return k.rows, nil
}

func (k *keyKnowledgeLookup) GetKnowledgeBatchWithSharedAccess(ctx context.Context, tenantID uint64, ids []string) ([]*types.Knowledge, error) {
	return k.GetKnowledgeBatch(ctx, tenantID, ids)
}

type keyKnowledgeBases struct {
	interfaces.KnowledgeBaseService
	rows []*types.KnowledgeBase
}

func (k *keyKnowledgeBases) GetKnowledgeBasesByIDsOnly(context.Context, []string) ([]*types.KnowledgeBase, error) {
	return k.rows, nil
}

func keyFileServiceContext() context.Context {
	ctx := context.WithValue(context.Background(), types.TenantIDContextKey, uint64(9))
	ctx = types.WithCaller(ctx, types.Caller{TenantID: 9, UserID: "reader"})
	return types.WithTenantAPIKeyScope(ctx, types.TenantAPIKeyScope{
		KeyID: 7, KnowledgeBaseIDs: types.StringArray{"kb-allowed"},
	})
}

func TestRestrictedKeyResolvesActualFileOnlyTargetsWithoutAgentExpansion(t *testing.T) {
	files := &keyKnowledgeLookup{rows: []*types.Knowledge{
		{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
	}}
	svc := &sessionService{
		knowledgeService: files,
		knowledgeBaseService: &keyKnowledgeBases{rows: []*types.KnowledgeBase{
			{ID: "kb-allowed", TenantID: 9},
		}},
	}
	req := &types.QARequest{
		Session:      &types.Session{TenantID: 9},
		KnowledgeIDs: []string{"doc-1"},
		CustomAgent: &types.CustomAgent{TenantID: 9, Config: types.CustomAgentConfig{
			KBSelectionMode: "selected", KnowledgeBases: []string{"kb-allowed", "kb-blocked"},
		}},
	}
	ctx, err := svc.AuthorizeKnowledgeTargets(keyFileServiceContext(), nil, req.KnowledgeIDs)
	if err != nil {
		t.Fatal(err)
	}
	kbIDs, ids, err := svc.resolveKnowledgeBases(ctx, req)
	if err != nil || len(kbIDs) != 0 || len(ids) != 1 || ids[0] != "doc-1" {
		t.Fatalf("unexpected scope kbs=%v ids=%v err=%v", kbIDs, ids, err)
	}
	targets, err := svc.buildSearchTargets(ctx, 9, kbIDs, ids, nil)
	if err != nil || len(targets) != 1 || targets[0].Type != types.SearchTargetTypeKnowledge ||
		len(targets[0].KnowledgeIDs) != 1 || targets[0].KnowledgeIDs[0] != "doc-1" {
		t.Fatalf("file-only target was broadened: targets=%v err=%v", targets, err)
	}
}

func TestRestrictedFileVerificationChecksKnowledgeBaseTenant(t *testing.T) {
	for _, kbTenant := range []uint64{0, 10} {
		svc := &sessionService{
			knowledgeService: &keyKnowledgeLookup{rows: []*types.Knowledge{
				{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
			}},
			knowledgeBaseService: &keyKnowledgeBases{rows: []*types.KnowledgeBase{
				{ID: "kb-allowed", TenantID: kbTenant},
			}},
		}
		if _, err := svc.AuthorizeKnowledgeTargets(keyFileServiceContext(), nil, []string{"doc-1"}); err == nil {
			t.Fatalf("accepted a document bound to KB in tenant %d", kbTenant)
		}
	}
}

func TestDetachedOrResumedFileRequestRevalidatesCurrentOwnership(t *testing.T) {
	files := &keyKnowledgeLookup{rows: []*types.Knowledge{
		{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
	}}
	svc := &sessionService{
		knowledgeService: files,
		knowledgeBaseService: &keyKnowledgeBases{rows: []*types.KnowledgeBase{
			{ID: "kb-allowed", TenantID: 9},
		}},
	}
	ctx, err := svc.AuthorizeKnowledgeTargets(keyFileServiceContext(), nil, []string{"doc-1"})
	if err != nil {
		t.Fatal(err)
	}
	detached := logger.CloneContext(ctx)
	if types.AuthorizeTenantAPIKeyKnowledgeTargets(detached, nil, []string{"doc-1"}) == nil {
		t.Fatal("detached context inherited the first request's file proof")
	}
	if _, err := svc.AuthorizeKnowledgeTargets(detached, nil, []string{"doc-1"}); err != nil || files.calls != 2 {
		t.Fatalf("fresh authorization failed err=%v calls=%d", err, files.calls)
	}
	files.rows[0].KnowledgeBaseID = "kb-blocked"
	if _, err := svc.AuthorizeKnowledgeTargets(keyFileServiceContext(), nil, []string{"doc-1"}); err == nil {
		t.Fatal("a persisted/resumed file ID bypassed changed ownership")
	}
}
