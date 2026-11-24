package sandbox

import "industrialagent.local/knowledge-engine/internal/sandbox/workspace"

// ResolveWorkspacePathIn anchors a relative path at the layout root.
func ResolveWorkspacePathIn(l WorkspaceLayout, value string) string {
	return workspace.ResolvePathIn(l, value)
}
