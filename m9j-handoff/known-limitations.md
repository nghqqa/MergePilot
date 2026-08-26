# M9-J known limitations (honest record)

1. **First-Start transient on dev machine.** The first `Start` after a
   fresh offline Install failed once with
   `WINDOWS_LOOPBACK_PUBLICATION_FAILED: page=200 api=200 exited=True`
   (a container exited during the wait window; publication itself was
   already proven by page=200/api=200). An immediate retry with zero
   manual intervention started the stack cleanly, and a later
   Stop → re-Start cycle succeeded first-try. The test machine should
   retry once if this appears; if it reproduces consistently, report it
   as a defect (candidate for M9-K).

2. **Legacy dev-tool path literals in the full tools/ bundle.** The
   ZIP carries the complete `tools/` tree (required for standalone CLI
   operation). Some legacy dev scripts and one doc contain
   machine-path references in usage comments/defaults
   (`wsl -- bash /mnt/d/goai/tools/...` examples,
   `audit_trail.py` dev default, ROLLBACK.md historical example).
   These bytes are identical to RC.1–RC.5 (externally accepted), are
   not secrets, and are not runtime state — but they ARE machine-path
   literals, recorded here rather than claimed absent. Two further
   scanner hits are the secret-scanner's own detection regexes
   (guard code, not secrets).

3. **pgvector is never owner-deleted.** By design: the install journal
   records only the 8 `mergepilot-isolated-*` images.
   `pgvector/pgvector:pg16` (shared upstream base) is loaded but never
   claimed, so Cleanup never deletes it. Machines that want a fully
   blank cache must remove it manually.

4. **Install `-ImageTar` accepts only backslash Windows paths** (the
   WSL drvfs translation regex is backslash-form). Forward-slash
   invocation fails with `cannot translate path` (fail-closed, no
   partial import). `rerun-command.txt` uses the backslash form.

5. **Docker noise during Cleanup.** `docker image-inspect rc=1` /
   `Error response from daemon` lines can appear while the verified-ID
   removal resolves recorded refs across storage backends; final state
   is owner images deleted / non-owner retained / truthful report.

6. **Deferred work (unchanged from M9-C):** evidence git-blob hashing
   (§5) and PR#4 hygiene replay (§6) remain out of scope;
   `revision_producer_contract` / `audit_producer_contract` stay
   NOT_VERIFIED pending full-stack runtime verification.

## Truth boundaries (frozen — this candidate does NOT flip any)

application_integration_verified=false
database_verified=false
production_verified=false
revision_producer_contract=NOT_VERIFIED
audit_producer_contract=NOT_VERIFIED
direct_routing_verified=false
transport_profile=wsl-user-relay
