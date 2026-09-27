# Troubleshooting Runbook

This runbook collects the investigation paths for common failures.

## Database Unreachable

The symptom is the interface returning a database-unavailable error. Check in
order: the container health status, the host and port in the connection
string, and finally the credentials. Query errors during an outage are
counted in process memory and can be reconciled with the database log after
recovery.

## Index Invalidation

The symptom is queries returning the index-stale state. The cause is a
mismatch between the active model digest or version and the index rows. Fix
by re-ingesting the corpus to produce new version rows, or by rolling back to
a previous version inside the retention window.

## Embedding Timeout

The symptom is queries returning provider-unavailable. The local embedding
model never triggers this state; only an environment with a remote embedding
endpoint can. Check the endpoint address and timeout settings, and fall back
to the local model if necessary.

## Uncited Hits

The symptom is a hit count lower than the candidate row count with a drop
counter. This is the citation contract at work: rows missing line numbers are
dropped deliberately. Check whether the ingestion pipeline recorded the line
fields correctly.
