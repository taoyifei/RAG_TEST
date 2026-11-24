// Package sessioncmd holds `knowledgeengine session` command tree (list / view /
// delete / ask / resume / stop) for chat history and agent invocation.
//
// Package name `sessioncmd` (not `session`) so callers can `import sdk
// "industrialagent.local/knowledge-engine/client"` and use `sdk.Session` without
// shadowing - same hygiene as `profilecmd`.
package sessioncmd

import (
	"github.com/spf13/cobra"

	"industrialagent.local/knowledge-engine/cli/internal/cmdutil"
)

// NewCmd builds the `knowledgeengine session` parent command.
func NewCmd(f *cmdutil.Factory) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "session",
		Short: "Manage chat sessions",
	}
	cmd.AddCommand(NewCmdList(f))
	cmd.AddCommand(NewCmdView(f))
	cmd.AddCommand(NewCmdDelete(f))
	cmd.AddCommand(NewCmdAsk(f))
	cmd.AddCommand(NewCmdResume(f))
	cmd.AddCommand(NewCmdStop(f))
	cmd.AddCommand(NewCmdToolApproval(f))
	return cmd
}
