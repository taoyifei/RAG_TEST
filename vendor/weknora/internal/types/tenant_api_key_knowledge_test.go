package types

import (
	"context"
	"testing"
)

func knowledgeKeyTestContext() context.Context {
	ctx := WithCaller(context.Background(), Caller{TenantID: 9, UserID: "reader"})
	return WithTenantAPIKeyScope(ctx, TenantAPIKeyScope{
		KeyID: 7, KnowledgeBaseIDs: StringArray{"kb-allowed"},
	})
}

func TestVerifiedKnowledgeTargetsAreBoundToKeyCallerAndExactIDSet(t *testing.T) {
	ctx := knowledgeKeyTestContext()
	ids := []string{"doc-1", "doc-2"}
	lookups := 0
	verified, err := VerifyTenantAPIKeyKnowledgeTargets(ctx, nil, ids,
		func(_ context.Context, tenantID uint64, actualIDs []string) ([]*Knowledge, error) {
			lookups++
			if tenantID != 9 || len(actualIDs) != 2 {
				t.Fatalf("unexpected lookup tenant=%d ids=%v", tenantID, actualIDs)
			}
			return []*Knowledge{
				{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
				{ID: "doc-2", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
			}, nil
		})
	if err != nil || lookups != 1 {
		t.Fatalf("verify err=%v lookups=%d", err, lookups)
	}
	if err := AuthorizeTenantAPIKeyKnowledgeTargets(verified, nil, []string{"doc-2", "doc-1", "doc-1"}); err != nil {
		t.Fatalf("same verified set rejected: %v", err)
	}
	for _, unverifiedIDs := range [][]string{{"doc-1"}, {"doc-1", "doc-3"}, {"doc-1", "doc-2", "doc-3"}} {
		if AuthorizeTenantAPIKeyKnowledgeTargets(verified, nil, unverifiedIDs) == nil {
			t.Fatalf("different file set accepted: %v", unverifiedIDs)
		}
	}
	if AuthorizeTenantAPIKeyKnowledgeTargets(ctx, []string{"kb-allowed"}, ids) == nil {
		t.Fatal("client KB declaration must not replace server verification")
	}
	changed := []context.Context{
		WithCaller(verified, Caller{TenantID: 10, UserID: "reader"}),
		WithCaller(verified, Caller{TenantID: 9, UserID: "different-reader"}),
		WithTenantAPIKeyScope(verified, TenantAPIKeyScope{KeyID: 8, KnowledgeBaseIDs: StringArray{"kb-allowed"}}),
		WithTenantAPIKeyScope(verified, TenantAPIKeyScope{KeyID: 7, KnowledgeBaseIDs: StringArray{"kb-other"}}),
	}
	for i, replaced := range changed {
		if AuthorizeTenantAPIKeyKnowledgeTargets(replaced, nil, ids) == nil {
			t.Fatalf("proof survived changed caller/key/scope case %d", i)
		}
	}
	if AuthorizeTenantAPIKeyKnowledgeTargets(verified, []string{"kb-other"}, ids) == nil {
		t.Fatal("verified files must not authorize an unrelated whole KB")
	}
}

func TestKnowledgeTargetVerificationFailsClosedForEveryBinding(t *testing.T) {
	cases := []struct {
		name string
		rows []*Knowledge
	}{
		{"missing", nil},
		{"nil row", []*Knowledge{nil}},
		{"outside allowlist", []*Knowledge{{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-blocked"}}},
		{"cross tenant", []*Knowledge{{ID: "doc-1", TenantID: 10, KnowledgeBaseID: "kb-allowed"}}},
		{"missing KB", []*Knowledge{{ID: "doc-1", TenantID: 9}}},
		{"different ID", []*Knowledge{{ID: "doc-2", TenantID: 9, KnowledgeBaseID: "kb-allowed"}}},
		{"duplicate rows", []*Knowledge{
			{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
			{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
		}},
		{"mixed IDs", []*Knowledge{
			{ID: "doc-1", TenantID: 9, KnowledgeBaseID: "kb-allowed"},
			{ID: "doc-2", TenantID: 10, KnowledgeBaseID: "kb-allowed"},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ctx := knowledgeKeyTestContext()
			result, err := VerifyTenantAPIKeyKnowledgeTargets(ctx, nil, []string{"doc-1"},
				func(context.Context, uint64, []string) ([]*Knowledge, error) { return tc.rows, nil })
			if err == nil {
				t.Fatal("unverified file binding accepted")
			}
			if AuthorizeTenantAPIKeyKnowledgeTargets(result, nil, []string{"doc-1"}) == nil {
				t.Fatal("failed verification left a usable proof")
			}
		})
	}
}

func TestKnowledgeProofIsNotClonedIntoDetachedWork(t *testing.T) {
	clone, declared := ContextCloneDecision(TenantAPIKeyKnowledgeProofContextKey)
	if !declared || clone {
		t.Fatal("detached QA must reload current file ownership rather than inherit a grant")
	}
}
