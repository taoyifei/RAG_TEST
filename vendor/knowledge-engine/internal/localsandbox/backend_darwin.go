//go:build darwin

package localsandbox

import (
	"context"

	"industrialagent.local/knowledge-engine/internal/localsandbox/core"
	"industrialagent.local/knowledge-engine/internal/localsandbox/seatbelt"
	"industrialagent.local/knowledge-engine/internal/logger"
)

// NewBackend returns the platform backend. The build tag on this file is the
// only place the OS is selected; no other code may branch on runtime.GOOS.
func NewBackend() (core.Backend, error) {
	logger.Infof(context.Background(), "[LocalSandbox] using seatbelt backend")
	return seatbelt.New()
}
