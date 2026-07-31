# M4-D verification evidence

Status: implementation, deterministic verification, and real fixture E2E
verification complete; awaiting independent review and release authorization.
Not committed, tagged, or pushed.

Scope:

- `skills/pr_lifecycle/`
- `tests/m4d/`
- `evidence/m4/m4d/`
- frozen design: `docs/M4-D-PRLifecycle设计冻结.md`

Current deterministic coverage:

- four high-level actions and Draft 2020-12 input/output contracts;
- deploy-owned role/repository/base/run/credential trust boundary;
- fix PR creation and idempotent reconciliation;
- same-idempotency payload conflict and non-allowlisted repository denial;
- modified/removed-file revert derivation and fail-closed unsupported states;
- coordinator-only merge/close with M3 L2 approval-ticket forwarding and
  read-only post-write reconciliation;
- pre-write denial versus post-forward effect-unknown mapping;
- production adapter normalization and common-runtime envelope integration.

Release blocker and release state:

- The real fixture GitHub E2E must run through
  `python -m skills.pr_lifecycle.run -> PolicyGatewayAdapter -> Policy Gateway
  -> github-mcp -> GitHub fixture`.
- `gateway-e2e.json` now passes the structured gate with 11 scenarios,
  including `close_pr`, and fixture residue is zero.
- Until independent review and explicit release authorization pass, M4-D must
  not be committed, tagged, or pushed.

Run the deterministic and release gates from Git Bash:

```text
bash tests/m4d/run_all.sh
```

The gate writes:

- `test-output-r1.txt`
- `test-output-r2.txt`
- `verification.txt`
- `gateway-e2e.json` (only from the real fixture E2E runner)
