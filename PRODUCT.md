# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing codebase answers it: stdlib-only Python HTTP server (`tools/demo_console/serve.py`)
behind a fixed-allowlist loopback edge proxy (`tools/demo_console/console_edge.py`),
serving static assets from `tools/demo_console/live_assets/` with no build step,
no framework, no third-party JS. The E2E status surface is a single static
page + one JSON endpoint.

## Users

MergePilot maintainers and E2E failure triagers. They open the console during or
after a GitHub E2E DAG run (17 stages) and must, within about 30 seconds,
judge: is the current run trustworthy, which stage failed (with the first
stable error code), is the network path correct (6 relay route edges), and is
there cleanup residue. They are engineers comfortable with stage names,
error codes, and journal vocabulary; they are not marketers or passers-by.

## Product Purpose

MergePilot is an isolated GitHub-integration E2E and demo stack. This surface
is its read-only operations console: a live, honest projection of the session
journal (`run_id`, stage progression, transport profile, receipt/matrix
verification, prerequisite counts, route-probe verdicts, relay resource
counts, residue) so a maintainer can assess run health without touching the
CLI or reading raw JSON. Success = a triager trusts what they see (no
synthesized states), finds the failing stage and its stable error immediately,
and never mistakes a stale or failed run for a complete one.

## Positioning

The single-source, fail-closed honesty of the underlying pipeline: every
displayed value is derived from the session journal by one whitelisted
projection writer, `journal_complete` is strict equality on the real
`e2e_stage`, absent data renders as unavailable rather than guessed, and
unverified truth boundaries stay visibly NOT_VERIFIED. No neighboring
dashboard can truthfully copy that guarantee because it is the pipeline's own
contract, not a UI layer's promise.

## Operating Context

Runs live on the isolated WSL2 stack (console-edge publishes loopback
127.0.0.1:8600). Data source: `GET /api/e2e/status` serving the CLI-written
derived projection (`/run/mergepilot/public/status.json`, mounted read-only);
the showcase view at `/` covers demo snapshots. E2E DAG stages are numbered
1–17 (prerequisites → networks → containers → firewall → relay → postgres →
bootstrap → probes → route probes → gateway → services → agents → receipt →
matrix → preflight → complete). Five truth boundaries
(application_integration_verified, database_verified, production_verified,
revision_producer_contract, audit_producer_contract) remain false /
NOT_VERIFIED until real production artifacts prove otherwise — the console
must keep showing them as such.

## Capabilities and Constraints

- Read-only, always: no apply/delete/rewire/rollback/production-write
  operations may exist anywhere in the console, and no write affordance may
  appear in the UI.
- Only real API fields are displayed. Fields the API does not provide render
  as 未提供/未验证, never fabricated.
- `available=false` must never display as complete; `direct_routing_verified=false`
  must never display as verified; stale data must be labeled stale.
- Edge proxy allows only fixed paths; API responses are `no-store`; the
  secrets directory is never mounted; no client-supplied journal paths; no
  arbitrary file reads; no forwarding of Authorization/Cookie/Proxy/
  X-Forwarded headers; no third-party JS, CDN, analytics, or remote fonts.
- System UI fonts only; fixed font sizes; letter-spacing 0; WCAG AA contrast;
  state must carry icon or text in addition to color.

## Brand Commitments

Name: MergePilot. Voice: quiet, professional, dense, scannable engineering
console. No gradients, glass effects, decorative glows/blobs, large heroes,
marketing copy, or card-in-card nesting. The page must not be a monochrome
deep-blue theme; the operator-fixed visual world is a light neutral workspace
with dark navigation structure and restrained green/amber/red status colors.

## Evidence on Hand

- run35 (b8-e2e-run35) real journal + output: `D:\goai\temp\m8gh4-run27\evidence\`
  (Stage 1–17 genuinely passed; transport wsl-user-relay; 6/6 route edges
  verified; receipt/matrix verified; 16/16 prerequisites).
- Maintenance baseline: full gates 2192 passed / 20 skipped; cleanup ownership
  closed; test pollution hermetic.
- Five truth boundaries: still false / NOT_VERIFIED. The console must not
  fabricate evidence or production-readiness claims; showcase/demo data is not
  production evidence.

## Product Principles

1. Honesty over reassurance: absent, stale, or unverified renders as exactly
   that; nothing is dressed up as complete or verified.
2. Thirty-second triage: run trust, failing stage, stable error, route
   verdicts, and residue are all reachable in one scan, no clicking required.
3. Single source of truth: the journal projection is the only data source; no
   second state machine inside the UI.
4. Read-only forever: the console observes; it never operates.
5. Density with calm: engineering density achieved through grouping and
   hierarchy, never through noise or decoration.

## Accessibility & Inclusion

Status must never rely on color alone (icon or text always accompanies it);
keyboard focus must be complete and visible; `prefers-reduced-motion` must be
honored; WCAG AA contrast for text and states.
