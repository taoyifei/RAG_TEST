package im_test

import (
	"context"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"industrialagent.local/knowledge-engine/internal/im"
	"industrialagent.local/knowledge-engine/internal/im/dingtalk"
	"industrialagent.local/knowledge-engine/internal/im/feishu"
	"industrialagent.local/knowledge-engine/internal/im/mattermost"
	"industrialagent.local/knowledge-engine/internal/im/qqbot"
	"industrialagent.local/knowledge-engine/internal/im/slack"
	"industrialagent.local/knowledge-engine/internal/im/telegram"
	"industrialagent.local/knowledge-engine/internal/im/yunzhijia"
)

func TestMissingWebhookSecretsFailClosed(t *testing.T) {
	for _, adapter := range []im.Adapter{
		&dingtalk.Adapter{}, &feishu.Adapter{}, &mattermost.Adapter{}, &qqbot.Adapter{},
		&slack.Adapter{}, &telegram.Adapter{}, &yunzhijia.Adapter{},
	} {
		ctx, _ := gin.CreateTestContext(httptest.NewRecorder())
		ctx.Request = httptest.NewRequest("POST", "/callback", nil)
		require.Error(t, adapter.VerifyCallback(ctx), "%T", adapter)
	}
}

func TestSlackRejectsAttackerDownload(t *testing.T) {
	// A nil API client also proves rejection happens before any SDK/network call.
	adapter := &slack.Adapter{}
	for _, target := range []string{
		"https://evil.example/download", "https://files.slack.com.evil.example/",
		"http://files.slack.com/file", "https://files.slack.com:444/file",
	} {
		_, _, err := adapter.DownloadFile(context.Background(), &im.IncomingMessage{
			FileKey: "file", Extra: map[string]string{"url_private_download": target},
		})
		require.Error(t, err)
	}
}

func TestDingtalkRejectsAttackerReply(t *testing.T) {
	adapter := &dingtalk.Adapter{}
	err := adapter.SendReply(context.Background(),
		&im.IncomingMessage{Extra: map[string]string{"session_webhook": "https://evil.example/"}},
		&im.ReplyMessage{Content: "synthetic"})
	require.Error(t, err)
}
