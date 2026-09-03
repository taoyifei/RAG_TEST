# Confidence and Abstention

P07 confidence is an explainable rule result, not a cross-provider raw-score
threshold. Inputs include exact coverage, channel agreement, rank stability,
same-model rerank margin, identifier coverage, citable span coverage, evidence
count and diversity, and degraded provider/index flags. Rank stability and
same-model margin are bounded diagnostic features; no raw score is compared
across providers.

Stable outcomes are `ANSWERABLE`, `INSUFFICIENT_EVIDENCE`,
`PROVIDER_UNAVAILABLE`, `POLICY_DENIED`, `INDEX_NOT_READY`, `INDEX_CORRUPT` and
`AMBIGUOUS_NEEDS_CLARIFICATION`. No evidence means no generator call. Metadata-
only evidence cannot independently produce answerable confidence.

Thresholds and weights are provisional. P08 alone may calibrate them against a
frozen tuning/holdout dataset and real providers; offline P07 tests demonstrate
contracts, failure closure and isolation rather than semantic quality.
