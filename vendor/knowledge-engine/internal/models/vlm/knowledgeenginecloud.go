package vlm

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
	"industrialagent.local/knowledge-engine/internal/logger"
	"industrialagent.local/knowledge-engine/internal/models/api"
	"industrialagent.local/knowledge-engine/internal/models/utils"
)

const (
	knowledgeEngineCloudVLMPath = "/api/v1/chat/completions"
	// maxKnowledgeEngineCloudVLMErrorPreviewBytes bounds how much of a failed response
	// is echoed into the returned error and the caller's logs.
	maxKnowledgeEngineCloudVLMErrorPreviewBytes = 4 << 10
)

// KnowledgeEngineCloudVLM implements VLM via the KnowledgeEngineCloud API.
type KnowledgeEngineCloudVLM struct {
	modelName       string
	remoteModelName string
	modelID         string
	appID           string
	apiKey          string
	baseURL         string
	client          *http.Client
}

// NewKnowledgeEngineCloudVLM creates a KnowledgeEngineCloud-backed VLM instance.
func NewKnowledgeEngineCloudVLM(config *Config) (*KnowledgeEngineCloudVLM, error) {
	if config.AppID == "" {
		return nil, fmt.Errorf("KnowledgeEngineCloud VLM: AppID is required")
	}
	if config.AppSecret == "" {
		return nil, fmt.Errorf("KnowledgeEngineCloud VLM: AppSecret is required")
	}
	baseURL := strings.TrimRight(config.BaseURL, "/")
	if err := validateVLMBaseURL(baseURL); err != nil {
		return nil, err
	}
	remoteModelName := ""
	if config.Extra != nil {
		if v, ok := config.Extra["remote_model_name"]; ok {
			if vs, ok := v.(string); ok {
				remoteModelName = strings.TrimSpace(vs)
			}
		}
	}
	return &KnowledgeEngineCloudVLM{
		modelName:       config.ModelName,
		remoteModelName: remoteModelName,
		modelID:         config.ModelID,
		appID:           config.AppID,
		apiKey:          config.AppSecret,
		baseURL:         baseURL,
		client:          newVLMHTTPClient(vlmHTTPTimeout()),
	}, nil
}

type knowledgeEngineCloudVLMContentPart struct {
	Type     string                           `json:"type"`
	Text     string                           `json:"text,omitempty"`
	ImageURL *knowledgeEngineCloudVLMImageURL `json:"image_url,omitempty"`
}

type knowledgeEngineCloudVLMImageURL struct {
	URL string `json:"url"`
}

type knowledgeEngineCloudVLMMessage struct {
	Role    string      `json:"role"`
	Content interface{} `json:"content"`
}

type knowledgeEngineCloudVLMRequest struct {
	Model       string                           `json:"model"`
	Messages    []knowledgeEngineCloudVLMMessage `json:"messages"`
	MaxTokens   int                              `json:"max_tokens,omitempty"`
	Temperature float64                          `json:"temperature,omitempty"`
	Stream      bool                             `json:"stream"`
}

type knowledgeEngineCloudVLMResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
}

// Predict sends images with a text prompt to the KnowledgeEngineCloud API.
func (v *KnowledgeEngineCloudVLM) Predict(ctx context.Context, imgBytesList [][]byte, prompt string) (string, error) {
	return v.PredictWithOptions(ctx, imgBytesList, prompt, nil)
}

// PredictWithOptions satisfies the interface. The hosted endpoint exposes no
// thinking switch, so the options are accepted and used for nothing — the same
// call goes out as Predict sends.
func (v *KnowledgeEngineCloudVLM) PredictWithOptions(
	ctx context.Context, imgBytesList [][]byte, prompt string, _ *PredictOptions,
) (string, error) {
	var parts []knowledgeEngineCloudVLMContentPart

	parts = append(parts, knowledgeEngineCloudVLMContentPart{
		Type: "text",
		Text: prompt,
	})

	for _, imgBytes := range imgBytesList {
		if len(imgBytes) > 0 {
			mimeType := detectImageMIME(imgBytes)
			b64 := base64.StdEncoding.EncodeToString(imgBytes)
			dataURI := fmt.Sprintf("data:%s;base64,%s", mimeType, b64)
			parts = append(parts, knowledgeEngineCloudVLMContentPart{
				Type: "image_url",
				ImageURL: &knowledgeEngineCloudVLMImageURL{
					URL: dataURI,
				},
			})
		}
	}

	reqBody := knowledgeEngineCloudVLMRequest{
		Model: v.effectiveModelName(),
		Messages: []knowledgeEngineCloudVLMMessage{
			{
				Role:    "user",
				Content: parts,
			},
		},
		MaxTokens:   defaultMaxToks,
		Temperature: float64(defaultTemp),
		Stream:      false,
	}

	bodyBytes, err := json.Marshal(reqBody)
	if err != nil {
		return "", fmt.Errorf("knowledgeenginecloud VLM: marshal: %w", err)
	}

	requestID := uuid.New().String()
	headers := utils.Sign(v.appID, v.apiKey, requestID, string(bodyBytes))

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, v.baseURL+knowledgeEngineCloudVLMPath, bytes.NewReader(bodyBytes))
	if err != nil {
		return "", fmt.Errorf("knowledgeenginecloud VLM: create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, hv := range headers {
		req.Header.Set(k, hv)
	}

	totalImageSize := 0
	for _, img := range imgBytesList {
		totalImageSize += len(img)
	}
	logger.Infof(ctx, "[VLM] Calling KnowledgeEngineCloud API, model=%s, baseURL=%s, numImages=%d, totalImageSize=%d",
		v.effectiveModelName(), v.baseURL, len(imgBytesList), totalImageSize)

	resp, err := v.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("knowledgeenginecloud VLM: do request: %w", err)
	}
	defer resp.Body.Close()

	respBytes, err := readKnowledgeEngineCloudVLMResponse(resp)
	if err != nil {
		return "", err
	}

	var vlmResp knowledgeEngineCloudVLMResponse
	if err := json.Unmarshal(respBytes, &vlmResp); err != nil {
		return "", fmt.Errorf("knowledgeenginecloud VLM: unmarshal: %w", err)
	}
	if len(vlmResp.Choices) == 0 {
		return "", fmt.Errorf("knowledgeenginecloud VLM: no choices in response")
	}

	content := vlmResp.Choices[0].Message.Content
	logger.Infof(ctx, "[VLM] KnowledgeEngineCloud response received, len=%d", len(content))
	return content, nil
}

// readKnowledgeEngineCloudVLMResponse reads a completed VLM call. The success body is
// the model's answer, so it goes through the same cap as every other
// non-streaming provider reply instead of being buffered whole. A failed body
// is a vendor error message that callers put in an error and a log line, so
// only a bounded preview is kept.
func readKnowledgeEngineCloudVLMResponse(resp *http.Response) ([]byte, error) {
	if resp.StatusCode != http.StatusOK {
		preview, _ := io.ReadAll(io.LimitReader(resp.Body, maxKnowledgeEngineCloudVLMErrorPreviewBytes))
		return nil, fmt.Errorf("knowledgeenginecloud VLM: status %d: %s",
			resp.StatusCode, trimPartialUTF8(string(preview)))
	}
	data, err := api.ReadResponseCapped(resp.Body, api.MaxResponseBytes)
	if err != nil {
		return nil, fmt.Errorf("knowledgeenginecloud VLM: read response: %w", err)
	}
	return data, nil
}

// trimPartialUTF8 drops a trailing incomplete rune. A preview stops on a byte
// count, so a multi-byte error body can end mid-rune; carrying that into an
// error string would put invalid UTF-8 into logs and API responses.
func trimPartialUTF8(s string) string {
	for len(s) > 0 && !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}

func (v *KnowledgeEngineCloudVLM) effectiveModelName() string {
	if v.remoteModelName != "" {
		return v.remoteModelName
	}
	return v.modelName
}

func (v *KnowledgeEngineCloudVLM) GetModelName() string { return v.modelName }
func (v *KnowledgeEngineCloudVLM) GetModelID() string   { return v.modelID }
