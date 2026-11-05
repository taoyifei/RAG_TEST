package chatpipeline

import (
	"context"
	"strings"

	"github.com/Tencent/WeKnora/internal/event"
	"github.com/Tencent/WeKnora/internal/types"
)

// 与正文共用一次生成，避免为每次问答另加一次模型调用。
const answerSupportPrompt = `本轮回答依据（内部协议，状态行由应用隐藏）：
先判断当前检索资料能否直接支持本轮回答，首行输出一种状态：
[[WXD_SUPPORT:supported]]：资料足以回答。随后正常回答，在事实段落附上实际使用的运行时引用。
[[WXD_SUPPORT:insufficient]]：资料不相关、不足或只支持部分问题。随后说明已知与未知的边界，不凑无关引用。
不能凭相似背景、示例或历史助手回答补造事实。闲聊可以自然、正向、适度风趣；资料不足不等于所有问题都必须拒答。正文遵循用户要求的语言和格式。`

const answerSupportPrefix = "[[WXD_SUPPORT:"

const answerSupportReminder = `先输出[[WXD_SUPPORT:supported]]或[[WXD_SUPPORT:insufficient]]状态行，再正常回答。资料能支持回答时，按运行时协议引用实际使用的片段，简短回答也必须附引用；资料不足则说明边界，不得补造事实或凑引用。状态行由应用隐藏，不向用户解释。`

// answerSupportDecoder 只暂存很短的状态前缀，正文仍逐段流式返回。
type answerSupportDecoder struct {
	enabled bool
	buffer  string
	decided bool
	emit    func(string)
}

func (d *answerSupportDecoder) Feed(content string) string {
	if !d.enabled || d.decided {
		return content
	}
	d.buffer += content
	text := strings.TrimLeft(d.buffer, " \t\r\n")
	if text == "" || strings.HasPrefix(answerSupportPrefix, text) {
		return ""
	}
	if strings.HasPrefix(text, answerSupportPrefix) {
		if end := strings.Index(text, "]]"); end >= 0 {
			status := text[len(answerSupportPrefix):end]
			if status != "supported" && status != "insufficient" {
				status = "unknown"
			}
			d.decide(status)
			return strings.TrimLeft(text[end+2:], "\r\n")
		}
		if len(text) < 128 {
			return ""
		}
		// 损坏的内部前缀不展示为用户正文，也不声明证据已充分。
		d.decide("unknown")
		return ""
	}
	content = d.buffer
	d.decide("unknown")
	return content
}

func (d *answerSupportDecoder) Flush() string {
	if !d.enabled || d.decided {
		return ""
	}
	content := d.buffer
	text := strings.TrimSpace(content)
	if strings.HasPrefix(answerSupportPrefix, text) || strings.HasPrefix(text, answerSupportPrefix) {
		content = ""
	}
	d.decide("unknown")
	return content
}

func (d *answerSupportDecoder) decide(status string) {
	d.buffer = ""
	d.decided = true
	if d.emit != nil {
		d.emit(status)
	}
}

func newAnswerSupportDecoder(ctx context.Context, cm *types.ChatManage) *answerSupportDecoder {
	return &answerSupportDecoder{
		enabled: len(cm.MergeResult) > 0,
		emit: func(status string) {
			if cm.EventBus == nil {
				return
			}
			_ = cm.EventBus.Emit(ctx, types.Event{
				Type:      types.EventType(event.EventAgentToolResult),
				SessionID: cm.SessionID,
				Data: event.AgentToolResultData{
					ToolName: "answer_support",
					Success:  status != "unknown",
					Data:     map[string]interface{}{"status": status},
				},
			})
		},
	}
}
