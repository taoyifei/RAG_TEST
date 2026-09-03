# Legacy Query Service Migration

The existing `rag_app.query_service.QueryService` and public HTTP schema remain
unchanged in P07. The new retrieval and answering services live under
`rag_app.application` and depend only on Core ports and models. An explicit
legacy adapter can translate a P07 result for development or shadow comparison.

The migration modes are `legacy`, `new` and `shadow`, but P07 does not silently
change the production default. Shadow output cannot affect the published answer
and must obey the same egress, scope, body-logging and budget policies. Removal
or public schema replacement is deferred to P09/P11 decisions.
