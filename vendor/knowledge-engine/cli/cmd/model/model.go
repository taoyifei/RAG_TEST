// Package modelcmd holds the `knowledgeengine model` command tree: list / view.
//
// These are read-only discovery commands. The model id is a required input to
// `knowledgeengine agent create --model <id>` and to a knowledge base's embedding /
// summary model settings, so the CLI must offer a first-class way to find it
// rather than forcing callers down to the raw `knowledgeengine api` escape hatch.
//
// The directory is named `model/` to match the cobra subcommand; the Go
// package is `modelcmd` to avoid colliding with the SDK's Model type.
package modelcmd

import (
	"github.com/spf13/cobra"

	"industrialagent.local/knowledge-engine/cli/internal/cmdutil"
	sdk "industrialagent.local/knowledge-engine/client"
)

// NewCmd builds the `knowledgeengine model` parent and registers leaves. Called from
// cli/cmd/root.go.
func NewCmd(f *cmdutil.Factory) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "model",
		Short: "Manage models (list / view / create / update / delete)",
		Long: `List, inspect, register, and delete the models configured on the server. Use
the model id to back a knowledge base's embedding / summary config ('knowledgeengine kb
init') or an agent ('knowledgeengine agent create --model <id>').`,
	}
	cmd.AddCommand(NewCmdList(f))
	cmd.AddCommand(NewCmdView(f))
	cmd.AddCommand(NewCmdCreate(f))
	cmd.AddCommand(NewCmdUpdate(f))
	cmd.AddCommand(NewCmdDelete(f))
	return cmd
}

// modelLabel prefers display_name, falling back to the raw name.
func modelLabel(m sdk.Model) string {
	if m.DisplayName != "" {
		return m.DisplayName
	}
	return m.Name
}
