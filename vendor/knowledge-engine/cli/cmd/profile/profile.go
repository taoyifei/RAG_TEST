// Package profilecmd holds the `knowledgeengine profile` command tree
// (list / add / remove / use).
//
// Package name `profilecmd` (not `profile`) to keep the pattern consistent
// with other cmd subpackages.
// The cobra Use: string is "profile" - this is what users type.
package profilecmd

import (
	"github.com/spf13/cobra"

	"industrialagent.local/knowledge-engine/cli/internal/cmdutil"
)

// NewCmd builds the `knowledgeengine profile` parent command.
func NewCmd(f *cmdutil.Factory) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "profile",
		Short: "Manage CLI profiles (named connection targets)",
	}
	cmd.AddCommand(NewCmdList(f))
	cmd.AddCommand(NewCmdAdd(f))
	cmd.AddCommand(NewCmdRemove(f))
	cmd.AddCommand(NewCmdUse(f))
	return cmd
}
