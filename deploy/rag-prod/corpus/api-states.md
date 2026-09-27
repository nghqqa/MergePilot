# Retrieval Service State Contract

The retrieval interface must honestly return one of six service states. No
implementation may disguise a failure as an empty result.

## The Six States

The hit state means matches exist and results are non-empty. The empty state
means the index is healthy but nothing matched. Model missing means the
requested embedding model is not registered. Index stale means index rows do
not match the active model digest or version. Provider unavailable means the
embedding service is unreachable. Error means an unexpected internal failure,
for example an unavailable database.

## Degradation Principle

When the service is unreachable, return an explicit degraded state with a
reason; the response header carries the service state marker. An empty result
and a degradation are two different states, and the frontend must display
them differently.

## Observability

Every query records its latency, state, hit count, and the number of dropped
uncited rows. Metrics are derived from the real query log; latency
percentiles are computed inside the database. When the database is unwritable
the error state is counted in process memory as a complement.
