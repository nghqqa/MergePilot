---
name: risk-classify
description: Classify a structured change context into an advisory L0/L1/L2 risk level with explainable reasons. Use after diff-parse to decide what review/approval controls to recommend. Deterministic, only-escalate, advisory-only (never an authorization decision).
---

# risk-classify · advisory risk aggregator

Deterministic, **advisory-only** classifier that turns a change context (e.g.
diff-parse output) plus a *versioned* declarative ruleset into an L0/L1/L2 risk
level with structured reasons. Pure compute: no Nacos, no network, no LLM, no
database, does not read `policy.yaml`, and does **not** use author/team/trust
level to lower risk.

## When to call

After diff-parse, when Triage/Coordinator/Fixer/Verifier need a risk level and a
list of recommended controls. It is a **suggestion**; the Policy Gateway is
always the final authorization authority.

## How to call

Either entry redacts + size-limits on every path (including pre-validation
errors):

```
python -m skills.risk_classify.run < request.envelope.json
# or via the common CLI:
python -m skills.common.runtime.cli --skill skills.risk_classify.run.handle < request.envelope.json
```

Both reuse the common runtime via `run_request`; all error emissions go through
`_finalize` (credential redaction + 1 MiB limit + schema check), so
credential-shaped content in a malformed request envelope never reaches stdout.

The business `input` is validated against
`skills/risk_classify/schema/input.schema.json`, and the business `output` is
validated against `skills/risk_classify/schema/output.schema.json` at the
production entry.

## Input (business `input`)

- `change_context` — DiffParse business output shape (needs `files`,
  `change_categories`, `stats`, `complete`)
- `risk_floor` — optional `L0`/`L1`/`L2` (default `L0`); the result can never go
  below this
- `expected_rules_version` — optional semver; if present and it differs from the
  loaded ruleset, the Skill errors (fail-closed)

## Output (business `output`, schema_version `"1"`)

`risk_level` (L0/L1/L2), `risk_rank` (0/1/2), `risk_floor`, `rules_version`,
`advisory_only` (always `true`), `reasons[]` (`rule_id`, `level`, `summary`,
`files`), `matched_rules[]`, `recommended_controls[]`
(`AUTO_REVIEW_ELIGIBLE` / `HUMAN_REVIEW` / `L2_APPROVAL_RECOMMENDED`),
`approval_recommended`.

## Rules

Declarative rules live in `skills/risk_classify/rules/risk-rules.v1.json`
(`rules_version` `"1.0.0"`), validated by
`skills/risk_classify/schema/rules.schema.json`. Predicates are AND-combined;
file-wise predicates match if any changed file satisfies them, context-wise
predicates match against the whole change context. **Thresholds live in the
rules file, not in code.**

## Hard guarantees

- **Only-escalate:** `risk_level = max(risk_floor, highest matched rule)`. It
  can never drop below the floor and rules can never lower it.
- **Deterministic:** identical `(change_context, rules_version)` → identical
  output; reordering rules or files cannot change the level.
- **Fail-closed:** missing/corrupt/unknown-version ruleset → `ERROR`
  (`RISK_CLASSIFY_RULES_MISSING` / `RISK_CLASSIFY_RULESET_INVALID` /
  `RISK_CLASSIFY_RULESET_VERSION_UNSUPPORTED`). It never silently falls back to
  a default that would lower risk.
- **Advisory only:** it outputs recommendations, never "approved"/"denied"/
  "merge". `side_effects` is empty.
