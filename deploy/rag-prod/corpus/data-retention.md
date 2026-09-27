# Data Retention and Versioning Policy

This document defines the retention windows and versioning rules for the
trial environment.

## Retention Window

Index rows are retained per version with a window of the two most recent
versions. Rows older than the window are pruned at ingestion time. A rollback
target must still be inside the retention window; otherwise the rollback
request is explicitly rejected.

## Document Lifecycle

A document has two states: active and deleted. Deleting a document physically
removes all of its index rows but keeps the document metadata row marked as
deleted for audit traceability. The content of a deleted document can never
be retrieved again.

## Raw Archival

Raw documents are archived content-addressed into object storage with the
document content digest as the key. Immediately after writing, the object is
read back and its digest verified; an object that fails verification may not
be referenced.

## Incremental Ingestion

Re-ingesting identical content is an idempotent no-op that produces no new
index rows. When content changes, only that document's index rows are
rebuilt; other documents are untouched.
