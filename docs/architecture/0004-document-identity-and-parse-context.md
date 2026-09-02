# 0004 Document Identity and Parse Context

Status: Accepted for P05.5

## Decision

Logical document versions are identified by both the globally unique logical
document ID and the content digest:

```text
document_version_id = deterministic_id("dver", document_id, content_sha256)
```

Content-level Blob identity remains `sha256:<content_sha256>`. A document
version is therefore not a Blob identity and equal bytes uploaded as two
logical documents cannot share document-version, node, or chunk IDs.

`ParseContext(document: DocumentRef)` carries project, knowledge-base,
document, and display-name context. `ParsingPolicy` contains only parsing
semantics and resource limits. Runtime scope and display names do not enter the
canonical policy or Index Fingerprint.

## Uniqueness contract

P05.5 adopts globally unique `document_id` semantics. Before persistence, a
DocumentRef creation/import boundary must call
`validate_document_ref_uniqueness()` and reject one `document_id` bound to a
different project or knowledge base. Reusing an ID in the same scope for a
renamed display name is allowed.

## Stability and fingerprint rules

- Same document ID, bytes, and policies produce the same dver, node, and chunk
  IDs.
- Renaming `display_name` does not change those IDs or the Index Fingerprint.
- Changing bytes changes the dver and affected downstream IDs.
- Changing parsing or chunking semantics changes the Index Fingerprint.
- Changing project, knowledge base, document ID, request ID, or trace ID does
  not change the ParsingPolicy canonical identity.

An architecture test rejects direct `deterministic_id("dver", ...)` calls in
business code outside the single Core helper.

## Compatibility

An old `sha256:<content>` or content-only dver is not the same identifier as the
new logical version. No production P06 database or vector index exists in this
phase, so no SQLite or Qdrant migration is performed. Legacy adapters remain
read-compatible but emit the new identity when converting into Core models.
