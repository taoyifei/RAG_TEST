package types

import (
	"context"

	"github.com/Tencent/WeKnora/internal/errors"
)

// TenantAPIKeyKnowledgeLookup 由服务端按调用方租户查询真实文件，不接受浏览器声明的归属。
type TenantAPIKeyKnowledgeLookup func(context.Context, uint64, []string) ([]*Knowledge, error)

type tenantAPIKeyKnowledgeProof struct {
	keyID    uint64
	caller   Caller
	bindings map[string]string
}

// VerifyTenantAPIKeyKnowledgeTargets 校验完整文件集合后产生仅当前上下文可用的凭据。
// lookup 必须读取真实资料及知识库归属；返回缺失、跨租户或越界文件时整体拒绝。
func VerifyTenantAPIKeyKnowledgeTargets(
	ctx context.Context, kbIDs, knowledgeIDs []string, lookup TenantAPIKeyKnowledgeLookup,
) (context.Context, error) {
	if err := AuthorizeTenantAPIKeyKnowledgeBases(ctx, kbIDs...); err != nil {
		return ctx, err
	}
	scope, restricted := TenantAPIKeyScopeFromContext(ctx)
	if !restricted || !scope.IsKnowledgeBaseRestricted() || len(knowledgeIDs) == 0 {
		return ctx, AuthorizeTenantAPIKeyKnowledgeTargets(ctx, kbIDs, knowledgeIDs)
	}
	if AuthorizeTenantAPIKeyKnowledgeTargets(ctx, kbIDs, knowledgeIDs) == nil {
		return ctx, nil
	}
	caller := CallerFromContext(ctx)
	ids := normalizeIDArray(StringArray(knowledgeIDs))
	if caller.TenantID == 0 || len(ids) == 0 || lookup == nil {
		return ctx, errors.NewForbiddenError("API key scope cannot verify knowledge file bindings")
	}
	requested := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		requested[id] = struct{}{}
	}
	rows, err := lookup(ctx, caller.TenantID, append([]string(nil), ids...))
	if err != nil {
		return ctx, err
	}
	bindings := make(map[string]string, len(ids))
	for _, row := range rows {
		if row == nil || row.TenantID != caller.TenantID || row.DeletedAt.Valid ||
			row.KnowledgeBaseID == "" || !scope.AllowsKnowledgeBase(row.KnowledgeBaseID) {
			return ctx, errors.NewForbiddenError("API key scope does not allow one or more knowledge files")
		}
		if _, ok := requested[row.ID]; !ok {
			return ctx, errors.NewForbiddenError("API key scope received an unexpected knowledge binding")
		}
		if _, duplicate := bindings[row.ID]; duplicate {
			return ctx, errors.NewForbiddenError("API key scope received an ambiguous knowledge binding")
		}
		bindings[row.ID] = row.KnowledgeBaseID
	}
	if len(bindings) != len(requested) {
		return ctx, errors.NewForbiddenError("API key scope could not verify all knowledge file bindings")
	}
	verified := context.WithValue(ctx, TenantAPIKeyKnowledgeProofContextKey, tenantAPIKeyKnowledgeProof{
		keyID: scope.KeyID, caller: caller, bindings: bindings,
	})
	return verified, AuthorizeTenantAPIKeyKnowledgeTargets(verified, kbIDs, knowledgeIDs)
}

func verifiedTenantAPIKeyKnowledgeTargets(ctx context.Context, scope TenantAPIKeyScope, knowledgeIDs []string) bool {
	proof, ok := ctx.Value(TenantAPIKeyKnowledgeProofContextKey).(tenantAPIKeyKnowledgeProof)
	if !ok || proof.keyID != scope.KeyID || proof.caller != CallerFromContext(ctx) {
		return false
	}
	ids := normalizeIDArray(StringArray(knowledgeIDs))
	if len(ids) == 0 || len(ids) != len(proof.bindings) {
		return false
	}
	for _, id := range ids {
		kbID, verified := proof.bindings[id]
		if !verified || !scope.AllowsKnowledgeBase(kbID) {
			return false
		}
	}
	return true
}
