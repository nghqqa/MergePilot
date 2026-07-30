---
name: diff-parse
description: Parse a caller-supplied real unified diff into a structured change context (files, change types, hunks, modules, categories, stats). Use whenever a PR/commit diff must be turned into structured data for downstream review or risk classification.
---

# diff-parse · unified-diff structured parser

Deterministic, framework-neutral parser that turns a **real unified diff text**
into a structured change context. Pure read/compute: no GitHub fetch, no
network, no database, no filesystem reads of the diff paths, no shell.

## When to call

Triage / Coordinator / Reviewer need structured per-file change data before
risk classification or review. The caller already has the diff text (from a
git/GitHub tool) and hands it in via `diff_text`.

## How to call

Either entry emits exactly one JSON response envelope on stdout and redacts +
size-limits on every path (including pre-validation errors):

```
python -m skills.diff_parse.run < request.envelope.json
# or via the common CLI:
python -m skills.common.runtime.cli --skill skills.diff_parse.run.handle < request.envelope.json
```

Both reuse the common runtime via `run_request`; all error emissions
(`run_request`, the direct `run.py` entry, and the common CLI's pre-validation
path) go through `_finalize` (credential redaction + 1 MiB limit + schema check),
so credential-shaped content in a malformed request envelope never reaches stdout.

The request envelope is contract version `"1"` (see
`skills/common/schema/request.envelope.schema.json`); the business `input` is
validated against `skills/diff_parse/schema/input.schema.json`, and the business
`output` is validated against `skills/diff_parse/schema/output.schema.json` at
the production entry.

## Input (business `input`)

- `repo` — `owner/name`
- `base_sha`, `head_sha` — 40-hex SHAs
- `diff_format` — `"unified"` (only supported value)
- `diff_text` — the real unified diff text
- `pr_number` — optional positive integer
- `options` — optional: `max_files`, `max_total_lines`, `max_diff_bytes`

## Output (business `output`, schema_version `"1"`)

`schema_version`, `source`, `input_sha256`, `complete`, `files[]`
(`path`, `old_path`, `change_type` A/M/D/R/C/T, `additions`, `deletions`,
`binary`, `mode_changed`, `categories[]`, `hunks[]`), `modules_touched[]`,
`change_categories[]`, `stats`. Output carries structure/ranges/stats/digest
only — **never** full source or patch text.

## Behavior & boundaries

- Statuses: `OK` (fully parsed) / `PARTIAL` (a soft file/line cap was hit —
  explicit `warning_codes` + `degradations`, never a silent drop) / `ERROR`
  (`DIFF_PARSE_UNSUPPORTED_FORMAT`, `DIFF_PARSE_INPUT_TOO_LARGE`,
  `DIFF_PARSE_MALFORMED`; common codes otherwise).
- Fail-closed on malformed/truncated diffs and on input over `max_diff_bytes`;
  it never fabricates a `complete` result.
- Diff content (paths, hunk bodies, prompt-injection text, secret-shaped
  strings) is untrusted opaque text: never executed, never interpreted as an
  instruction, and never echoed in the output.
- `side_effects` is empty; no persistent side effects.
- Deterministic: identical input yields byte-identical business output.
