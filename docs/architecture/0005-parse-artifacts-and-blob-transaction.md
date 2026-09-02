# 0005 Parse Artifacts and Blob Transaction

Status: Accepted for P05.5

## Decision

Parser adapters are pure with respect to persistent storage:

```text
bytes + ParsingPolicy + ParseContext
  -> DocumentIR + ParseReport + ParsedArtifact[]
```

`ParsedArtifact` binds `artifact_id`, SHA-256, media type, bytes, and a
`source_document` or `embedded_media` role. `DocumentSource.blob_ref` and image
Blob references point to artifact IDs. Equal embedded media is returned once
as an artifact while each image occurrence remains a separate IR node.

Parsers do not construct BlobStore implementations, own Store lifecycle, or
call put/delete. A parser failure therefore cannot leave a persistent Blob
write behind.

## P06 transaction contract

`BlobStorePort` exposes:

- `put_if_absent(request) -> CREATED | EXISTING`
- `read(blob_id)`
- `exists(blob_id)`
- `delete(blob_id)`
- `close()`

`persist_artifacts_transactionally()` deduplicates identical artifact IDs,
records each `CREATED` result, and on failure deletes only those newly created
objects in reverse order. It never deletes an `EXISTING` object. A conflicting
artifact identity fails closed.

P06 ingestion must use this contract before committing document/revision
metadata. It must not restore the old parser-owned Blob lifecycle.

## Scope

The in-memory implementation proves the contract only. P05.5 does not add a
formal SQLite Blob catalog, Qdrant writes, reference counting, or garbage
collection.
