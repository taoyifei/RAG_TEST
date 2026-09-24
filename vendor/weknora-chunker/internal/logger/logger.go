// Package logger 为固定版本分块器提供仅使用标准库的日志桥接。
package logger

import (
	"context"
	"log/slog"
)

// Debugf 保留上游分块器仅用于诊断的日志调用。
func Debugf(ctx context.Context, format string, args ...any) {
	slog.DebugContext(ctx, format, args...)
}
