# Knowledge engine

This directory contains the Go retrieval service, the Vue administration UI and
the Python document reader used by Industrial Agent. Build the full stack
with the [deployment guide](../../deployment/industrial-agent/README.md).

To build the server from this directory with Go 1.26:

```sh
go build -o KnowledgeEngine ./cmd/server
```

For in-process office parsing, prepare the Rust library with `make anydoc-lib`,
then build with `go build -tags anydoc -o KnowledgeEngine ./cmd/server`.
The container build uses the same source and supports both variants.

See the [component lock](../../upstream.lock.json), [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for source provenance,
copyright, license terms and dependencies.
