# Phase P05.5 Progress

## Scope

P05.5 hardens the P00-P05 contracts required before persistent ingestion. It
does not implement P06 SQLite/Qdrant migration, complete P07 search wiring, or
new business functionality.

## Delivered contracts

- Logical dver identity includes global document ID and content SHA-256.
- Runtime ParseContext is separate from semantic ParsingPolicy.
- Composition retains actual parsing/chunking policies and derives required
  embedding slots and limits from topology.
- Provider profile fields reach strict adapter configs and actual request JSON.
- Parsers return content-addressed Artifacts without BlobStore side effects.
- Artifact persistence rolls back only objects created by its own attempt.
- Cache identity is role-policy sensitive; partial cache fills only gaps.
- Query failover checks the standby circuit before shared token estimation and
  budget reservation.
- DOCX hyperlink, traversal, Part hashing, IR invariant, and redaction checks are
  hardened.
- Tables preserve empty/omitted columns and continuous header rows.
- Chunk quality metrics are calculated and adversarially tested.
- Jina Reranker validates the full response, preserves equal-score RRF order,
  and returns only `request.limit` items.

## Evidence boundary

Stage-branch validation on 2026-09-02:

| Command | Result |
| --- | --- |
| compileall | PASS |
| `ruff check .` | PASS |
| mypy, no incremental | PASS, 209 source files |
| `pytest -q tests/midterm_hardening` | PASS, 3 passed |
| Core/Composition/Application/Adapters | PASS, 250 passed |
| `scripts/dev.py check` | PASS, 1205 passed, 75 deselected, 4 warnings |
| `scripts/dev.py smoke` | PASS, 62 passed, 1 warning |
| `git diff --check` | PASS |

The warnings are existing dependency/runtime-preflight warnings and do not hide
test failures. All provider tests use injected fakes or MockTransport. The
authoritative Git SHAs and post-merge rerun are recorded in the final P05.5
handoff.

External services actually called: none.

This phase does not claim retrieval-quality improvement or real Provider
availability.
