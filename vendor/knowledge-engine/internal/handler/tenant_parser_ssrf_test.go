package handler

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"industrialagent.local/knowledge-engine/internal/types"
)

func TestValidateParserEngineOutboundURLs_RejectsPrivateMinerUEndpoint(t *testing.T) {
	err := validateParserEngineOutboundURLs(&types.ParserEngineConfig{
		MinerUEndpoint: "http://127.0.0.1:8080",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "mineru_endpoint")
}

func TestValidateParserEngineOutboundURLs_RejectsPrivateVLMServerURL(t *testing.T) {
	err := validateParserEngineOutboundURLs(&types.ParserEngineConfig{
		MinerUVLMServerURL: "http://169.254.169.254",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "mineru_vlm_server_url")
}
