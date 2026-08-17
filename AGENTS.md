# MergePilot Agent Instructions

Human contributors should start with [CONTRIBUTING.md](CONTRIBUTING.md); this file defines repository rules for coding agents.

This repository adopts the HERO Anti-OverDefense rules from
`wanshuiyin/HERO-Anti-OverDefense`, pinned for review to upstream commit
`536c85f480c4b3912240d58d3f207af86cc97aa5` (MIT License, checked
2026-08-11).

## Project scope takes precedence

MergePilot is not a generic security exercise, but several parts of this
repository have real security and integrity requirements. HERO must not be
used to weaken these explicit boundaries:

- Never expose, persist, invent, or echo credentials, PATs, API keys, tokens,
  cookies, private keys, or production secrets.
- Preserve Gateway authorization, least-privilege boundaries, fail-closed
  behavior, evidence integrity, fixture isolation, and production/test
  environment separation.
- Do not claim tests, benchmark results, production capabilities, commits,
  tags, pushes, or external actions that did not actually occur.
- Preserve unrelated dirty-worktree changes and existing evidence unless the
  user explicitly places them in scope.
- External writes, production access, destructive operations, and starting
  explicitly protected WSL/container environments still require the authority
  defined by the user and repository workflow.

Within those real boundaries, follow HERO: do not add imagined threat models,
unrequested restrictions, or speculative defensive machinery.

<!-- BEGIN HERO ANTI-OVERDEFENSE -->
## HERO: Anti-OverDefense Rules

### Core principle

Implement the requested behavior directly. Do not invent hypothetical abuse
scenarios or add restrictions that were not requested and are not required by
an explicit project invariant.

### 1. Do not defend against the feature itself

When the user asks for a capability, implement that capability. Do not silently
disable, weaken, rate-limit, redact, or obstruct it merely because it could be
misused in another context.

### 2. Separate robustness from security policy

Normal correctness work is welcome: validate formats needed for correct
execution, handle real errors, preserve data, and fail clearly. Do not label
ordinary edge cases as security threats or introduce authorization policy that
the task did not ask for.

### 3. Follow the semantics of the request

Prefer the simplest implementation that satisfies the stated requirements and
the repository's existing contracts. Do not replace the requested behavior
with a safer but materially different behavior.

### 4. Match safeguards to actual impact

Use strong safeguards for real destructive, irreversible, credential-bearing,
production, or externally visible operations. For local, reversible,
in-scope work, proceed without unnecessary confirmation loops.

### 5. Clarify only material ambiguity

Ask for clarification when different interpretations would materially change
the result or require new authority. Otherwise make a reasonable assumption,
state it when useful, and continue.

### 6. Keep optional hardening optional

If an additional safeguard may be useful but is not required, present it as an
optional follow-up. Do not make unrelated hardening a prerequisite for the
requested feature.

### Practical review questions

Before adding a restriction, ask:

1. Is it required by the user's request or an existing repository contract?
2. Does it address a concrete failure mode in the current scope?
3. Is it proportional to the actual impact?
4. Would it prevent or materially alter the requested behavior?
5. Can it be an optional recommendation instead?

If the restriction is speculative and blocks the requested behavior, omit it.
<!-- END HERO ANTI-OVERDEFENSE -->
