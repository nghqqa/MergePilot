---
name: sast-scan
description: Deterministic, deduplicated static-analysis findings (secret leaks, Python-AST injection/dangerous calls, offline dependency advisories). Use on Reviewer first scan and Verifier re-scan after a fix. Outputs structured findings; never raw secrets.
---

# sast-scan · structured SAST findings

Framework-neutral core (stdlib + jsonschema) reusing the M4-A common runtime.
Pure read/compute: no network, no GitHub, no DB, no writes. Deterministic,
deduplicated, fail-closed.

## When to call

Reviewer first scan of changed files; Verifier re-scan after a fix. SecretScan +
DepVulnCheck are **sub-capabilities** (in-process engines), not separate Skills.

## How to call

```
python -m skills.sast_scan.run < request.envelope.json
# or via the common CLI:
python -m skills.common.runtime.cli --skill skills.sast_scan.run.handle < request.envelope.json
```

All emissions go through `_finalize` (redaction + 1 MiB limit + schema check).

## Input (business `input`)

- `mode` — `inline` (preferred; no host access) or `paths`.
- `files: [{path, content}]` — inline mode.
- `paths: [relative]` — paths mode, resolved **only** against the deploy-provided
  trusted root (`MERGEPILOT_SAST_WORKSPACE`); the request must **never** carry a
  host-absolute root. `..`/absolute/symlink/junction → fail-closed.
- `expected_rules_version`, `options` (max_bytes_per_file, max_total_bytes,
  max_findings, max_files — may only LOWER the frozen hard limits). v1 always
  runs all three engines; there is no caller-selectable `engines` field.

## Output (business `output`, schema_version `"1"`)

`findings[]` (`finding_id` `finding-<digest16>`, `fingerprint` sha256, `engine`,
`rule_id`, `category`, `severity`, `risk_level`, `file`, `line`/`column`,
`message`, `remediation`, `evidence_digest` sha256), `stats`, `engines_used`,
`dep_vuln_meta` (`db_version`/`source`/`covered_ecosystems`/`valid_until`/`stale`),
`complete`, optional `degraded`. Secrets never appear: messages use static rule
labels and `evidence_digest` is one-way.

## Rules

Versioned in `skills/sast_scan/rules/sast-rules.v1.json` (`rules_version 1.0.0`),
validated by `schema/rules.schema.json`. Invalid regex / duplicate rule_id /
unknown enum / unknown major version → `INTERNAL_ERROR` (fail-closed). The dep
advisory set is a small **offline local** DB, not complete/real-time; a past
`valid_until` → `PARTIAL + complete=false`.

## Status / errors (generic codes; subcode rides in `message`)

`OK` (complete) / `PARTIAL` (soft cap or stale DB; `complete=false`,
**not** fail-closed — downstream must not treat as "scan passed") / `ERROR`.
`INVALID_INPUT`(2): bad input / path escape / over `max_total_bytes`.
`DENIED`(4): `paths` mode without deploy trusted root. `INTERNAL_ERROR`(1):
ruleset corrupt / required engine failed / output schema mismatch.

## Determinism / dedup

`fingerprint = sha256(canonical_json([engine, rule_id, file, line, column,
evidence_digest, match_ordinal]))`; `match_ordinal` disambiguates same-position
findings without exposing secrets. Findings sorted by
`(file, line, column, rule_id, engine)`; identical input → byte-identical output.
`side_effects` is empty.
