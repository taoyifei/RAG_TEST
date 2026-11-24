// Package messagecmd implements `knowledgeengine message` — inspect and manage the
// messages inside chat sessions (the multi-turn substrate behind session ask).
package messagecmd

import (
	"github.com/spf13/cobra"

	"industrialagent.local/knowledge-engine/cli/internal/cmdutil"
)

// NewCmd builds the `knowledgeengine message` command group.
func NewCmd(f *cmdutil.Factory) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "message",
		Short: "Inspect and manage messages inside chat sessions",
	}
	cmd.AddCommand(NewCmdList(f))
	cmd.AddCommand(NewCmdSearch(f))
	cmd.AddCommand(NewCmdDelete(f))
	return cmd
}
