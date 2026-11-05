package chatpipeline

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAnswerSupportPrefixAcrossEveryStreamBoundary(t *testing.T) {
	for _, status := range []string{"supported", "insufficient"} {
		text := "[[WXD_SUPPORT:" + status + "]]\n正文与图片 ![图](resource://original)"
		for cut := 0; cut <= len(text); cut++ {
			var states []string
			decoder := &answerSupportDecoder{enabled: true, emit: func(s string) { states = append(states, s) }}
			body := decoder.Feed(text[:cut]) + decoder.Feed(text[cut:]) + decoder.Flush()
			require.Equal(t, "正文与图片 ![图](resource://original)", strings.TrimLeft(body, "\n"))
			require.Equal(t, []string{status}, states)
		}
	}
}

func TestAnswerSupportMissingOrInvalidPrefixIsNotVerified(t *testing.T) {
	for _, text := range []string{"普通正文", "[[WXD_SUPPORT:invented]]\n普通正文", "[[WXD_SUP", "[[WXD_SUPPORT:supported"} {
		var states []string
		decoder := &answerSupportDecoder{enabled: true, emit: func(s string) { states = append(states, s) }}
		body := decoder.Feed(text) + decoder.Flush()
		require.Equal(t, []string{"unknown"}, states)
		require.NotContains(t, body, "WXD_SUPPORT")
		if text == "普通正文" {
			require.Equal(t, text, body)
		}
	}
}

func TestAnswerSupportDoesNotChangeNoRetrievalResponse(t *testing.T) {
	decoder := &answerSupportDecoder{enabled: false}
	require.Equal(t, "轻松的闲聊", decoder.Feed("轻松的闲聊")+decoder.Flush())
}
