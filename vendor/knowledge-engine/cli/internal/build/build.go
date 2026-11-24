// Package build holds compile-time-injected build metadata.
//
// Values are set via -ldflags="-X industrialagent.local/knowledge-engine/cli/internal/build.Version=..." at link time;
// the Makefile's `version` target wires them up.
package build

var (
	Version = "dev"
	Commit  = "none"
	Date    = "unknown"
)

// Info returns a snapshot of the build metadata.
func Info() (version, commit, date string) {
	return Version, Commit, Date
}
