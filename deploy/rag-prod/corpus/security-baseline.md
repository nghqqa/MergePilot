# Security Baseline

This document defines the minimum security requirements. Violating any item
must be recorded as a high-risk finding.

## Credential Management

Secrets must never be committed to the repository; they are injected through
environment variables. No real credential, token, or private key may appear
in version control. Placeholder credentials in scripts must carry an obvious
local-trial marker.

## Audit Requirements

Audit events must carry an actor field. Actions whose actor cannot be
determined must be refused rather than written with an empty actor. Audit
records are append-only and must not be tampered with.

## Access Control

Deny all cross-repository access by default. The repository allowlist is the
only source of authorization. The repository list carried in a session must
come from server-side configuration; the frontend has no right to change it.

## Data Boundaries

Production data and trial data are physically isolated. A trial stack must
never mount production volumes, and a production stack must never read trial
indexes. The only exchange between the two environments is a manually
exported and reviewed static file.
