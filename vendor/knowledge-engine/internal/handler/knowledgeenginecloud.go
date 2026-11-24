package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"industrialagent.local/knowledge-engine/internal/types"
	"industrialagent.local/knowledge-engine/internal/types/interfaces"
)

// Keeps types imported so swag can resolve KnowledgeEngineCloudStatusResult.
var _ types.KnowledgeEngineCloudStatusResult

// KnowledgeEngineCloudHandler 处理 KnowledgeEngineCloud 凭证管理
type KnowledgeEngineCloudHandler struct {
	svc interfaces.KnowledgeEngineCloudService
}

// NewKnowledgeEngineCloudHandler 构造函数
func NewKnowledgeEngineCloudHandler(svc interfaces.KnowledgeEngineCloudService) *KnowledgeEngineCloudHandler {
	return &KnowledgeEngineCloudHandler{svc: svc}
}

type knowledgeEngineCloudCredentialsRequest struct {
	AppID     string `json:"app_id"     binding:"required"`
	AppSecret string `json:"app_secret" binding:"required"`
}

// SaveCredentials POST /api/v1/knowledgeenginecloud/credentials
// 仅保存 APPID/APPSECRET 凭证到空间配置，不自动创建模型
//
// SaveCredentials godoc
// @Summary      保存 KnowledgeEngineCloud 凭证
// @Description  保存 APPID/APPSECRET 到当前空间配置（不自动创建模型）
// @Tags         KnowledgeEngineCloud
// @Accept       json
// @Produce      json
// @Param        request  body      map[string]interface{}  true  "{app_id, app_secret}"
// @Success      200      {object}  handler.MessageResponse  "success: true"
// @Failure      400      {object}  map[string]interface{}  "请求参数错误"
// @Security     Bearer
// @Security     ApiKeyAuth
// @Router       /knowledgeenginecloud/credentials [post]
func (h *KnowledgeEngineCloudHandler) SaveCredentials(c *gin.Context) {
	var req knowledgeEngineCloudCredentialsRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	if err := h.svc.SaveCredentials(c.Request.Context(), req.AppID, req.AppSecret); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, gin.H{"success": true, "message": "凭证保存成功"})
}

// Status GET /api/v1/models/knowledgeenginecloud/status
// 检查当前空间的 KnowledgeEngineCloud 凭证是否完好，如需重新初始化则返回 needs_reinit=true
//
// Status godoc
// @Summary      检查 KnowledgeEngineCloud 凭证状态
// @Description  检查当前空间的 KnowledgeEngineCloud 凭证是否完好；needs_reinit=true 表示需要重新保存
// @Tags         KnowledgeEngineCloud
// @Produce      json
// @Success      200  {object}  types.KnowledgeEngineCloudStatusResult  "凭证状态"
// @Failure      500  {object}  map[string]interface{}  "服务器错误"
// @Security     Bearer
// @Security     ApiKeyAuth
// @Router       /models/knowledgeenginecloud/status [get]
func (h *KnowledgeEngineCloudHandler) Status(c *gin.Context) {
	result, err := h.svc.CheckStatus(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, result)
}
