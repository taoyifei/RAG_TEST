package chatpipeline

import (
	"context"
	"strings"

	"github.com/Tencent/WeKnora/internal/event"
	"github.com/Tencent/WeKnora/internal/types"
)

// 与正文共用一次生成，避免为每次问答另加一次模型调用。
const answerSupportPrompt = `本轮资料依据判断（内部协议，不属于回答正文）：
在回答前先判断当前检索资料是否能直接回答用户真正的问题。首行必须且只能输出以下状态之一：
[[WXD_SUPPORT:supported]]：资料明确支持本轮所回答的主体和事实。正文用运行时引用协议标注实际使用的来源，不得凑无关引用。
[[WXD_SUPPORT:insufficient]]：资料只是相似、仅有背景线索、没有相应事实，或只支持问题中的一部分。正文说明已知与未知的边界。
检索命中、标题相近、出现“我/我的”、同一园区或单位都不等于有答案。不能用工作地点推断用户家庭住址、当前位置，不能猜测人员身份、职务或私人信息。历史助手的说法也不是已核实的证据。
资料不足的业务问题应简洁说明缺少什么，可请用户补充；闲聊和主观评价仍可友好、正向、适度风趣，但不能编造外貌、经历、身份、位置等客观事实。不得声称已执行未执行的操作。
状态行之后正常回答，正文继续遵循用户要求的语言和格式。不解释内部状态，不为满足格式而引用不能支持答案的资料。`

const answerSupportPrefix = "[[WXD_SUPPORT:"

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
