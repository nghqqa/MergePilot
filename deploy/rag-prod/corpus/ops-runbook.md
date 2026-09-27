# Deployment and Rollback Runbook

This runbook describes the deployment and rollback procedures. Every
operation must leave an audit trail.

## Pre-Deployment Checks

Before every deployment the rollback anchor digest must be verified. Images
are pinned by digest; floating tags are not allowed. Only after confirming
the currently running version and the target version may a rolling update
proceed.

## Canary Rollout

The canary rollout proceeds in batches. Each batch requires five minutes of
metric observation, covering error rate, latency percentiles, and the audit
write rate. If any metric crosses its threshold, the rollout pauses and an
alert fires.

## Rollback Steps

The first step of a rollback is to point the image reference back to the
previous verified digest, then perform a normal update. Data volumes are
backward compatible, so a rollback needs no data migration. After the
rollback completes, the reason and the operator must be registered in the
audit system.

## Prohibitions

Never run an unverified image in production. Never skip health checks and
force a success mark. Never start a new rollout inside a rollback window.
