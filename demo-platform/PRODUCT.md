# PRODUCT.md — MergePilot Demo Platform

> Source: inferred entirely from the explicit Phase 14.2H-WD-DEMO-PLATFORM-BUILD brief (labeled assumptions: none — every requirement below is quoted from the build spec).

## What this is

MergePilot is a multi-agent code & security-change review and controlled-remediation platform. The demo platform is a **standalone judge-facing console** that replays two real AgentTeams/CoPaw cases from locked evidence:

- **PR #1 (normal)**: Reviewer → Fixer → Verifier, fully autonomous, no human gate.
- **PR #2 (high-risk)**: CWE-22 path traversal found → system pauses → human approves → minimal fix → independent verification → PR stays OPEN.

## Audience & scene

Competition judges and a live presenter. Used on a **projector in a bright room at 16:9**, recorded for submission, and browsed at 1440px desktop plus mobile. A judge must understand what the product does **within 10 seconds** of the first viewport.

## Visitor mode

**Operate** (control console): the presenter drives a replay (play / pause / next event / jump to human gate / reset / switch case / switch Replay↔Live); judges read status truth. Scanability, honest state legibility, and zero-ambiguity risk signaling outrank expression.

## The one mechanism to prove

High-risk findings force a **human gate**: agents stop, a human approves, then fix + verify proceed — all states, events, tasks, and approvals come from **real evidence** (never frontend-fabricated). Secondary proofs: SHA256 integrity, honest residual-risk disclosure, "REPLAY ACTION — NO RUNTIME WRITE" safety labeling.

## Hard constraints (design-relevant)

- Dark technical console; high-contrast status colors; text + color for every state (never color-only).
- Minimal, purposeful motion only; no fake 3D, no decorative backgrounds.
- Risk must dominate visually on PR #2 (HIGH RISK FOUND / CWE-22 / HUMAN VERIFICATION REQUIRED).
- Every page has loading, empty, error, and Replay-state treatments; footer shows data mode + update time.
- Not-connected features display honestly, never faked: PolarDB = NOT CONNECTED (Branch = SIMULATED fixture), RAG = SYNTHETIC demo dataset, per-task AgentLoop trace = 待接入 (evidence-replay). Platform-level AgentLoop Cloud Trace = LIVE CLOUD VERIFIED (historical authoritative trace, confirmed sample n=1 — not real-time data).

## Success looks like

A judge watching the PR #2 replay feels the **pause**: the timeline stops at the gate, the fixer shows LOCKED, one approval action unblocks the DAG — and the presenter never has to explain the visual hierarchy.
