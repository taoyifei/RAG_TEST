package wiki

import "industrialagent.local/knowledge-engine/internal/datasource/connector/feishu/core"

func txt(s string) *core.BlockText {
	return &core.BlockText{Elements: []core.TextElement{{TextRun: &core.TextRun{Content: s}}}}
}
