# a0e4e87 Contract Ruling

## Forensic evidence

```
commit:  a0e4e87bd2fd3a6e4a6612309fa4fad7c7fc33d3
parent:  55e55f6fd1a91247ede1208bf1345638464efd1a (= REV2)
message: "fix(download): realpath confinement (SEC-001, pipeline fixer REV2)"
files:   0 files changed (git show --stat shows no file lines)
diff:    empty (git diff 55e55f6..a0e4e87 produces no output)
tree:    fe8827ea8371bde972f2706e601b68ddeffee6d0 (IDENTICAL to 55e55f6's tree)
```

## Classification: **B — retry duplicate commit** (pre-guard defect)

Evidence:
1. **GitHub write call DID execute**: The GitHub `create_or_update_file` API was
   called with the same content and the CURRENT blob SHA (which, after REV2 was
   already written, pointed at REV2's blob). GitHub's API unconditionally creates
   a new commit object even when the content is unchanged.
2. **No idempotency check existed at the time**: The pre-write read-back was
   added by commit `bab7aae` (finding G fix) AFTER this duplicate was observed.
   The tool at the time of a0e4e87's creation had no "already at target" check.
3. **A new commit SHA was produced**: a0e4e87 is a distinct commit object from
   55e55f6 (different SHA, same tree) — GitHub created it because the API was
   called; this is not a client-side no-op marker.

## Ruling

a0e4e87 is **retained on the PR branch as defect evidence** (finding G). It is
NOT counted as a pipeline fix revision. The fix for the underlying defect
(idempotency pre-check in gh_fix_branch.py) is commit `bab7aae`. The post-fix
guarded retry is a verified no-op (commit count stays at 3).

## NOT classification A

It is NOT a "no-op reconciliation marker" because:
- It was not produced by an idempotent guard hitting a "already at target" match
- It carries no explicit NO_OP_REPLAY marking
- It was produced by a real GitHub API write call that should not have been made
