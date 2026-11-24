package service

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"industrialagent.local/knowledge-engine/internal/logger"
	"industrialagent.local/knowledge-engine/internal/models/providers"
	modelsutils "industrialagent.local/knowledge-engine/internal/models/utils"
	"industrialagent.local/knowledge-engine/internal/types"
	"industrialagent.local/knowledge-engine/internal/types/interfaces"
	"industrialagent.local/knowledge-engine/internal/utils"
)

type knowledgeEngineCloudService struct {
	tenantRepo interfaces.TenantRepository
}

// NewKnowledgeEngineCloudService 构造 KnowledgeEngineCloudService
func NewKnowledgeEngineCloudService(
	repo interfaces.ModelRepository,
	tenantRepo interfaces.TenantRepository,
) interfaces.KnowledgeEngineCloudService {
	return &knowledgeEngineCloudService{
		tenantRepo: tenantRepo,
	}
}

func IsKnowledgeEngineCloudDocReaderAddr(addr string) bool {
	readerURL := strings.TrimRight(providers.KnowledgeEngineCloudBaseURL, "/") + "/api/v1/doc/reader"
	return strings.TrimSuffix(strings.TrimSpace(addr), "/") == readerURL
}

// SaveCredentials 仅保存 APPID/APPSECRET 凭证，不自动创建模型
func (s *knowledgeEngineCloudService) SaveCredentials(ctx context.Context, appID, appSecret string) error {
	if appID == "" {
		return fmt.Errorf("app_id is required")
	}
	if appSecret == "" {
		return fmt.Errorf("app_secret is required")
	}

	if err := s.verifyCredentials(ctx, appID, appSecret); err != nil {
		return fmt.Errorf("credential verification failed: %w", err)
	}

	tenantID := types.MustTenantIDFromContext(ctx)
	return s.updateTenantCredentials(ctx, tenantID, appID, appSecret)
}

// verifyCredentials 向 KnowledgeEngineCloud /api/v1/health 发送带签名头的 GET。
//
// 注意：health 一般为探活接口，远端常不校验 APPID/SECRET 或签名；HTTP 200 通常只表示
// 「网关/服务可达」，不能严格证明凭证有效。若需强校验，应改为调用必须鉴权的业务接口。
func (s *knowledgeEngineCloudService) verifyCredentials(ctx context.Context, appID, appSecret string) error {
	baseURL := strings.TrimRight(providers.KnowledgeEngineCloudBaseURL, "/")
	healthURL := baseURL + "/api/v1/health"

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, healthURL, nil)
	if err != nil {
		return fmt.Errorf("create verification request failed: %w", err)
	}

	requestID := fmt.Sprintf("verify-%d", time.Now().UnixNano())
	signHeaders := modelsutils.Sign(appID, appSecret, requestID, "{}")
	for k, v := range signHeaders {
		req.Header.Set(k, v)
	}

	logger.Infof(ctx, "credential verification request: method=GET url=%s app_id=%s request_id=%s ",
		healthURL, appID, requestID)

	clientCfg := utils.DefaultSSRFSafeHTTPClientConfig()
	clientCfg.Timeout = 10 * time.Second
	client := utils.NewSSRFSafeHTTPClient(clientCfg)
	resp, err := client.Do(req)
	if err != nil {
		logger.Warnf(ctx, "credential verification HTTP failed: url=%s err=%v", healthURL, err)
		return fmt.Errorf("service unreachable: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return fmt.Errorf("invalid APPID or APPSECRET (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("invalid response status code: %d", resp.StatusCode)
	}
	return nil
}

// CheckStatus 检查 KnowledgeEngineCloud 凭证是否可正常解密
func (s *knowledgeEngineCloudService) CheckStatus(ctx context.Context) (*types.KnowledgeEngineCloudStatusResult, error) {
	tenantID := types.MustTenantIDFromContext(ctx)

	tenant, err := s.tenantRepo.GetTenantByID(ctx, tenantID)
	if err != nil || tenant == nil {
		return &types.KnowledgeEngineCloudStatusResult{HasModels: false, NeedsReinit: false}, nil
	}

	creds := tenant.Credentials.GetKnowledgeEngineCloud()
	if creds == nil {
		return &types.KnowledgeEngineCloudStatusResult{HasModels: false, NeedsReinit: false}, nil
	}

	// CredentialsConfig.Scan already attempts decryption.
	// If the AES key has rotated, Scan silently keeps the enc:v1:... blob.
	if strings.HasPrefix(creds.AppSecret, utils.EncPrefix) {
		return &types.KnowledgeEngineCloudStatusResult{
			HasModels:   true,
			NeedsReinit: true,
			Reason:      "KnowledgeEngineCloud 凭证解密失败（服务重启后加密密钥已变更），请重新填写 APPID 和 APPSECRET",
		}, nil
	}

	return &types.KnowledgeEngineCloudStatusResult{HasModels: true, NeedsReinit: false}, nil
}

// updateTenantCredentials 更新空间的 KnowledgeEngineCloud 凭证
func (s *knowledgeEngineCloudService) updateTenantCredentials(ctx context.Context, tenantID uint64, appID, appSecret string) error {
	if s.tenantRepo == nil {
		return fmt.Errorf("tenant repository is required")
	}

	tenant, err := s.tenantRepo.GetTenantByID(ctx, tenantID)
	if err != nil {
		return err
	}
	if tenant.Credentials == nil {
		tenant.Credentials = &types.CredentialsConfig{}
	}
	tenant.Credentials.KnowledgeEngineCloud = &types.KnowledgeEngineCloudCredentials{
		AppID:     appID,
		AppSecret: appSecret,
	}
	return s.tenantRepo.UpdateTenant(ctx, tenant)
}
