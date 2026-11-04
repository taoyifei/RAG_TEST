package service

import (
	"context"

	"github.com/Tencent/WeKnora/internal/errors"
	"github.com/Tencent/WeKnora/internal/types"
)

// AuthorizeKnowledgeTargets 仅为真实归属于 Key 所在租户且在允许库中的文件建立凭据。
func (s *sessionService) AuthorizeKnowledgeTargets(
	ctx context.Context, kbIDs, knowledgeIDs []string,
) (context.Context, error) {
	return types.VerifyTenantAPIKeyKnowledgeTargets(ctx, kbIDs, knowledgeIDs,
		func(ctx context.Context, tenantID uint64, ids []string) ([]*types.Knowledge, error) {
			if s.knowledgeService == nil || s.knowledgeBaseService == nil {
				return nil, errors.NewForbiddenError("knowledge scope verification is unavailable")
			}
			rows, err := s.knowledgeService.GetKnowledgeBatch(ctx, tenantID, ids)
			if err != nil {
				return nil, errors.NewInternalServerError("failed to verify knowledge file scope")
			}
			kbIDs := make([]string, 0, len(rows))
			for _, row := range rows {
				if row == nil || row.TenantID != tenantID || row.KnowledgeBaseID == "" {
					return nil, errors.NewForbiddenError("knowledge files must belong to the API key tenant")
				}
				kbIDs = append(kbIDs, row.KnowledgeBaseID)
			}
			kbs, err := s.knowledgeBaseService.GetKnowledgeBasesByIDsOnly(ctx, uniqueNonEmptyStrings(kbIDs))
			if err != nil {
				return nil, errors.NewInternalServerError("failed to verify knowledge base ownership")
			}
			allowed := make(map[string]bool, len(kbs))
			for _, kb := range kbs {
				if kb != nil && kb.TenantID == tenantID && !kb.DeletedAt.Valid {
					allowed[kb.ID] = true
				}
			}
			for _, row := range rows {
				if !allowed[row.KnowledgeBaseID] {
					return nil, errors.NewForbiddenError("knowledge base must belong to the API key tenant")
				}
			}
			return rows, nil
		},
	)
}
