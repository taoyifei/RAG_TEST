# Chunk Quality Report

## Source accounting

The report derives metrics from final SourceSpans and the Document IR. The
denominator is the exact text length of citable nodes after excluding policy
declared metadata-only comments and headers/footers. Intervals are merged per
node so overlap does not inflate unique coverage.

| Metric | Computation |
| --- | --- |
| total citable chars | Sum of eligible exact-text lengths |
| unique covered chars | Per-node union of citable source ranges |
| missing chars | Total minus unique coverage |
| source span coverage | Unique divided by total, or 1 for an empty denominator |
| duplicated citable chars | Referenced length minus interval union length |
| cross section/group | Chunks containing more than one section or source group |
| table row/cell coverage | Represented row ancestors and all physical cells in represented rows |
| list marker coverage | Actual DERIVED_NUMBERING spans |
| orphan/missing references | Unrepresented relationships, child groups, and note refs |
| ID/token violations | Duplicate chunk IDs and per-slot token-limit excesses |

`ChunkingReport` validates its own totals, coverage ratio, boundary aggregate,
and represented-count bounds. Adversarial tests bypass normal chunk creation to
prove missing spans, duplicate ranges, cross-section data, missing child groups,
and bad note refs are detected.

## Table views

Table rows are serialized from logical column positions. Empty and omitted
columns retain their position:

```text
citation:  A |  | C
embedding: [列1] A | [列2] <EMPTY> | [列3] C
```

`<EMPTY>` and `<OMITTED>` are retrieval context only. They never receive a
citable SourceSpan. Continuous `tblHeader` rows form a stable multi-line header
path instead of replacing one another.

## Validation and complexity

Source anchors must equal target-node anchors, ranges must fit exact text,
derived numbering must match the list marker, child/note references must exist,
and chunks cannot cross section or source-group boundaries. Separators remain
non-citable and quote publication cannot cross a separator or source.

Neighbor linking uses a group map. XML traversal uses start/end events and
fixed timeout checkpoints. These checks avoid repeated full scans and fragile
absolute-time assertions.
