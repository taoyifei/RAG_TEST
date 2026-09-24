// Package docparser 提供固定分块器依赖的图片链接展开函数。
// 实现复制自上游提交 1edcd54 的 internal/infrastructure/docparser/image_resolver.go。
package docparser

import "regexp"

var reLinkedImage = regexp.MustCompile(
	`\[!\[([^\]]*)\]\(([^()\s]*(?:\([^)]*\)[^()\s]*)*)\)\]` +
		`\([^()\s]*(?:\([^)]*\)[^()\s]*)*\)`,
)

// UnwrapLinkedImages 只移除图片外层的 Markdown 链接。
func UnwrapLinkedImages(markdown string) string {
	return reLinkedImage.ReplaceAllString(markdown, "![$1]($2)")
}
