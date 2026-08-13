# M7-P3 Project Portfolio Profile

**Status**: Active (positioning material for resume / interview / competition)
**Milestone**: M7-P3
**Scope rule**: Every statement below is tagged with what it is. The four tags
are used consistently:

- **[IMPL&VERIFIED]** — implemented and verified with evidence + git tag.
- **[DESIGNED]** — designed (design-freeze doc) but not implemented.
- **[OFFLINE-BENCH]** — verified by an offline / isolated benchmark only.
- **[ISOLATED]** — verified in the MergePilot-Test isolated stack, not production.
- **[PROD-CAP]** — production capability (real upstream live verification).

When in doubt, default to the weaker tag. See `M7-P3-Claim-Matrix.md` for the
authoritative boundary list.

---

## 1. One-line project intro

**Chinese**: MergePilot 是一个以确定性控制面为核心的多 Agent PR 审修与风险治理闭环——不止提意见，而是把 PR 从审查推进到受治理的修复、验证、审批与回滚，全程结构化审计。

**English**: MergePilot is a deterministic-control-plane multi-agent system that
closes the loop on pull requests — review, governed fix, verify, approve, and
rollback — with structured audit end to end, instead of stopping at "here are
some findings."

---

## 2. Resume description (Chinese, 80–120 chars)

> 基于 PostgreSQL 状态机+Outbox 的确定性控制面，编排多 Agent 完成 PR 审查、修复、验证、审批与回滚闭环；自研最小权限 Policy Gateway 与 6 类 Skill DAG，全程 OTel 可观测、pgvector 审计与 SHA 证据链，已通过真实 GitHub MCP 与隔离栈十轮稳定性验证。

Character count: ~118 chars (within 80–120). Trim to taste:

- ~95-char variant: 确定性控制面编排多 Agent 完成 PR 审修闭环（审查→修复→验证→审批→回滚），最小权限 Gateway + 6 Skill DAG，全程结构化审计与 SHA 证据链。

---

## 3. Technical highlights

1. **Deterministic control plane as the single source of truth** `[IMPL&VERIFIED]`
   - PostgreSQL state machine + Outbox owns task state, stage transitions,
     event de-dup, timeout, and crash recovery. The Agent runtime is **not**
     the state authority — an Agent crash is recovered by the state machine,
     not by re-prompting.
   - Evidence: B4e 43/43, B5 50/50, M3-C 33/33 (tags `m3b-b4e-closed`,
     `m3b-b5-closed`, `m3c-closed`).

2. **Minimum-privilege Policy Gateway with fail-closed semantics** `[IMPL&VERIFIED]`
   - A self-built Python SSE gateway sits between every Agent and every tool /
     GitHub call. Role-bound tokens, write constraints, full audit. Workers
     hold zero credentials — GitHub PAT lives only in a credential sidecar.
   - 8 classes of negative cases all fail-closed (50/50). The gateway is
     self-built, **not** Higress-native.

3. **6-skill deterministic DAG (subprocesses, not LLM free-form calls)** `[IMPL&VERIFIED]`
   - diff-parse, risk-classify, sast-scan, test-runner, pr-lifecycle,
     case-retrieval — 481 deterministic tests total. Skills are deterministic
     subprocesses with schema validation and size caps, not autonomous LLM
     tool calls.

4. **Real protocol E2E + rollback execution chain** `[IMPL&VERIFIED]`
   - Real GitHub MCP: PR #1 review → fix PR #3 → 5/5 resolved → squash merge.
   - Rollback chain: bad-fix commit → re-scan FAIL → revert commit → re-verify
     PASS. (Triggered by script today; M3-C child-run rollback covers the
     `POST_MERGE_VERIFY_FAILED` entry only.)

5. **Evidence-driven, SHA-verified Demo Console (REPLAY)** `[IMPL&VERIFIED]`
   - A read-only Console renders 8 pages from a single DemoBundle JSON. Every
     number traces to an evidence file with a SHA-256; the bundle itself is
     tamper-evident (`bundle_sha256` over canonical JSON).
   - REPLAY mode is implemented; **ISOLATED_LIVE is designed but not
     implemented** `[DESIGNED]`.

6. **RAG CaseRetrieval with held-out confirmatory benchmark** `[OFFLINE-BENCH]`
   - Real pgvector Docker E2E `all_passed=true` (169 tests). Held-out
     confirmatory benchmark on `rag-bench-v3-heldout` (25 cases, seed 99, zero
     overlap with calibration set) passes all pre-registered quality gates.
   - Boundary: benchmark uses a deterministic offline `TokenOverlapAdapter`;
     `runtime_consumes_rag_context=false`; RAG is advisory evidence only.

---

## 4. Technical difficulties (interview deep-dive)

Pick one or two depending on the role. Each has an honest "what is and isn't
solved" framing.

### 4.1 State authority vs. Agent autonomy
- **Problem**: LLM runtimes are unreliable at stage handoff, idempotency, and
  crash recovery. If the Agent runtime owns state, every crash is a data-loss
  event.
- **Solution**: Make PostgreSQL + Outbox the single source of truth; Agents are
  stateless consumers of work items. The Controller does de-dup, timeout,
  and recovery.
- **Honest boundary**: Manager stage handoff in early demos occasionally needed
  a human nudge; the M5-0B candidate workflow made handoffs deterministic
  (14/14 + 13/13) but **only on the isolated/candidate stack** `[ISOLATED]`.

### 4.2 Zero-credential Workers
- **Problem**: If Workers can read the GitHub PAT, any prompt-injection or
  hallucination can leak or misuse it.
- **Solution**: PAT lives only in a `github-mcp` credential sidecar; Workers
  call through `mcporter`; the Policy Gateway enforces per-role tool allowlists.
- **Honest boundary**: This is enforced in the gateway layer; it is **not** a
  formal secrets-management certification.

### 4.3 Fail-closed permission enforcement
- **Problem**: A permissive default turns every ambiguity into an allow.
- **Solution**: 8 classes of negative cases (unknown role, expired token,
  unauthorized tool, write-via-read-role, replayed token, malformed request,
  timeout, downstream error) all fail closed. 50/50 tests.
- **Honest boundary**: Gateway is self-built Python SSE, **not** Higress-native.

### 4.4 Docker socket proxy for upstream container auto-create (D2B-3)
- **Problem**: The upstream controller auto-creates containers without a
  disable switch, a privilege-escalation and disk-bloat risk.
- **Solution**: A fail-closed Docker socket proxy that gates container creation.
- **Honest boundary (resolved)**: Was `BLOCKED_UPSTREAM`; **now PASSED** with
  real AgentTeams v1.2.2 production live, 64/64, `hiclaw_live=true`
  `[PROD-CAP]`. The proxy is deployed; manager auto-create root-cause fix is a
  separate forward item.

### 4.5 Evidence provenance and replay determinism
- **Problem**: A demo that hardcodes numbers is unfalsifiable.
- **Solution**: DemoBundle JSON is the single source; SHA-256 per evidence
  file; `bundle_sha256` over canonical JSON; render-time verification; mode
  banner that cannot change at runtime.
- **Honest boundary**: This is **REPLAY** of a completed run, not live
  production data `[IMPL&VERIFIED]` for replay; ISOLATED_LIVE is `[DESIGNED]`.

### 4.6 RAG honesty under an unconsumed runtime
- **Problem**: It is tempting to claim "RAG improves review." The runtime does
  not consume RAG context, so that claim is not measurable.
- **Solution**: Label RAG as advisory evidence (`adopted=false`, `untrusted=true`),
  freeze `runtime_consumes_rag_context=false`, pre-register thresholds on a
  held-out set, and report `workflow_utility_status=NOT_MEASURABLE_WITH_CURRENT_RUNTIME`.
- **Honest boundary**: Benchmark is offline `TokenOverlapAdapter`
  `[OFFLINE-BENCH]`; no accuracy-improvement claim is made.

---

## 5. Likely interviewer questions (with honest answers)

**Q1: Is this deployed in production?**
A: The control plane, Policy Gateway, 6-skill DAG, and rollback chain are
implemented and verified `[IMPL&VERIFIED]`. D2B-3 (Docker socket proxy) is
verified against real AgentTeams v1.2.2 production live, 64/64 `[PROD-CAP]`.
The Demo Console you see is **REPLAY** of a completed run, not a live
production dashboard `[IMPL&VERIFIED]` for replay.

**Q2: Does RAG actually improve review quality?**
A: We do **not** claim that. Today `runtime_consumes_rag_context=false` —
`core.scan` and `core.run` do not read RAG context. RAG produces advisory
evidence with citations. The benchmark validates retrieval mechanics and
safety on a held-out set using a deterministic offline adapter
`[OFFLINE-BENCH]`, not production embedding quality or accuracy lift.

**Q3: Is the GitHub integration real?**
A: The protocol is real SSE. The Demo Console uses a **stateful fake GitHub
MCP** that speaks the real SSE protocol (protocol-real, not a call to
github.com). Separately, we have an earlier real-GitHub E2E: PR #1 → fix
PR #3 → 5/5 resolved → squash merge `[IMPL&VERIFIED]`.

**Q4: What is the benchmark's statistical power?**
A: Low — N=25 confirmatory cases (and N=10×2 for the single-vs-multi-Agent
formal benchmark). These are small-sample, controlled-local-orchestration
results `[OFFLINE-BENCH]`. They validate mechanics and safety, not
production-scale metrics. N≥20 confirmatory is met; broader scale is future
work.

**Q5: How is crash recovery handled?**
A: The PostgreSQL state machine + Outbox owns recovery. An Agent crash or
timeout is detected by the Controller; the work item is re-queued, not
re-prompted. Event de-dup prevents double-application `[IMPL&VERIFIED]`.

**Q6: Why did Manager handoff need human nudge?**
A: In early real-GitHub demos, the Manager orchestration handoff occasionally
stalled. The M5-0B candidate workflow (`reconcile_m5_skill_to_review`,
`reconcile_m5_handoffs`, `advance_m5_review_run`) made handoffs deterministic
(14/14 handoff + 13/13 concurrency/negative) on the **isolated/candidate
stack** `[ISOLATED]`. I will not generalize that to all production deployments.

**Q7: What is `findings=0` in the bundle?**
A: It is a storage artifact — the evidence store records **digests**, not
inline finding text. The findings table on screen is rendered from the
`findings[]` array, which itself points at a sourced evidence file. It does
**not** mean no issues were found.

**Q8: What is NOT done?**
A: (Be explicit.) M6-C real cloud SLS ingestion is not completed `[DESIGNED]`.
ISOLATED_LIVE Console mode is designed but not implemented `[DESIGNED]`.
Nacos / RocketMQ integration is planned, code not written `[DESIGNED]`. The
Demo Console is not a production management dashboard (no RBAC, no Agent
control, no GitHub writes) `[IMPL&VERIFIED]` as a read-only viewer.

---

## 6. Project titles

**Chinese (resume / competition)**:
- 主标题: MergePilot · 多 Agent PR 审修与风险治理闭环
- 副标题: 以确定性控制面编排审查、修复、验证、审批与回滚的全链可审计系统

**English (resume / GitHub)**:
- Main: MergePilot — A Deterministic Multi-Agent PR Review & Risk-Governance Loop
- Sub: Closing the loop from review to governed fix, verify, approve, and
  rollback, with structured audit end to end.

---

## 7. GitHub README project summary (3–4 sentences)

> MergePilot closes the loop on pull requests: instead of stopping at "here
> are some findings," a deterministic PostgreSQL control plane orchestrates
> multiple Agents through review, governed fix, verify, approve, and rollback.
> A minimum-privilege Policy Gateway enforces fail-closed tool access with
> zero credentials on Workers; a 6-skill deterministic DAG handles
> diff-parse, risk-classify, SAST, tests, PR lifecycle, and RAG case
> retrieval. Every claim is backed by SHA-256 evidence and replayable offline
> — the Demo Console is a read-only viewer, not a production dashboard. See
> `docs/M7-P3-Claim-Matrix.md` for the exact boundary between verified
> capabilities, offline benchmarks, and future work.

---

## 8. Competition "contributions and results" phrasing

Use the exact wording below; each clause is tagged so judges can match it to
evidence.

> **Contributions and results**
>
> - **Implemented and verified** `[IMPL&VERIFIED]`: a deterministic
>   PostgreSQL + Outbox control plane with L2 approval, failure recovery, and
>   a rollback execution chain (B4e 43/43, B5 50/50, M3-C 33/33).
> - **Implemented and verified** `[IMPL&VERIFIED]`: a minimum-privilege,
>   fail-closed Policy Gateway (8 negative classes, 50/50) and a 6-skill
>   deterministic DAG (481 tests).
> - **Implemented and verified** `[IMPL&VERIFIED]`: real-protocol AgentTeams
>   E2E (16/16 gates + 6/6 regression) and a real GitHub MCP loop
>   (PR #1 → PR #3, 5/5 resolved, squash merge).
> - **Production capability** `[PROD-CAP]`: D2B-3 fail-closed Docker socket
>   proxy verified on real AgentTeams v1.2.2 production live (64/64,
>   `hiclaw_live=true`).
> - **Offline / isolated verified** `[OFFLINE-BENCH]` / `[ISOLATED]`: RAG
>   CaseRetrieval (pgvector Docker E2E `all_passed=true`) and a held-out
>   confirmatory benchmark (25 cases, 16/16 pre-registered quality gates,
>   `confirmatory_all_ok=true`); HiClaw isolated C3 10/10 stability.
> - **Designed but not implemented** `[DESIGNED]`: ISOLATED_LIVE Console mode,
>   M6-C real cloud SLS ingestion, Nacos/RocketMQ integration.
> - **Explicit non-claims**: RAG is advisory only
>   (`adopted=false`, `untrusted=true`, `runtime_consumes_rag_context=false`);
>   the benchmark uses a deterministic offline adapter and does not claim
>   Reviewer/Fixer accuracy improvement; the Demo Console is a read-only
>   evidence viewer, not a production management dashboard.

---

## Tag legend (reproduced for standalone use)

| Tag | Meaning |
|-----|---------|
| `[IMPL&VERIFIED]` | Implemented and verified with evidence + git tag. |
| `[DESIGNED]` | Designed (design-freeze doc) but not implemented. |
| `[OFFLINE-BENCH]` | Verified by an offline / isolated benchmark only. |
| `[ISOLATED]` | Verified in MergePilot-Test isolated stack, not production. |
| `[PROD-CAP]` | Production capability — real upstream live verification. |
