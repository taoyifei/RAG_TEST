//go:build windows

package localsandbox

import (
	"industrialagent.local/knowledge-engine/internal/localsandbox/core"
	"industrialagent.local/knowledge-engine/internal/localsandbox/winhost"
)

func NewBackend() (core.Backend, error) { return winhost.New() }
