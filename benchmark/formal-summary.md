# MergePilot Formal Benchmark Summary (N=10x2)

> Post-run, machine-computed by `benchmark/freeze_formal_results.py`. Every number is recomputed from `benchmark/raw-runs/*.json`. Generated_at is the only non-deterministic field; re-running the freeze reproduces identical content otherwise.

- generated_at: `2026-08-11T11:08:05Z`
- summary_version: 1
- design: `controlled_local_pair_orchestration`
- model: `deepseek-v4-flash`  | timeout=120s  | token_budget=4096  | temperature=0.1

## 1. Infrastructure completion

| group | completed | n |
|---|---:|---:|
| A_single_agent | 10 | 10 |
| B_mergepilot | 10 | 10 |
| **total** | **20** | **20** (infrastructure completion rate = 100.00%) |

Infrastructure completion measures that the controlled local orchestration produced a parseable, schema-valid, completed run for every (case x group) pair. It is **not** the same as semantic case pass or E2E production completion.

## 2. Semantic case pass

| group | semantic pass | n |
|---|---:|---:|
| A_single_agent | 2 | 10 |
| B_mergepilot | 3 | 10 |
| **total** | **5** | **20** (semantic case pass rate = 25.00%) |

semantic case pass requires both the per-case decision and the finding-level evaluation to be correct; it is NOT the same as E2E production completion.

## 3. Finding-level metrics (TP / FP / FN / precision / recall / F1)

| group | TP | FP | FN | precision | recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| A_single_agent | 12 | 21 | 5 | 36.36% | 70.59% | 48.00% |
| B_mergepilot | 12 | 9 | 5 | 57.14% | 70.59% | 63.16% |

## 4. Decision accuracy (per-case decision == ground-truth)

| group | decision correct | n | decision accuracy |
|---|---:|---:|---:|
| A_single_agent | 5 | 10 | 50.00% |
| B_mergepilot | 4 | 10 | 40.00% |

## 5. Deltas (B vs A)

| metric | A | B | delta |
|---|---:|---:|---:|
| precision | 36.36% | 57.14% | +20.78 pp |
| recall | 70.59% | 70.59% | +0.00 pp |
| F1 | 48.00% | 63.16% | +15.16 pp |
| case pass | 20% | 30% | +10 pp |
| decision accuracy | 50.00% | 40.00% | -10.00 pp |
| false positives (FP) | 21 | 9 | -12 |
| tokens | 12062 | 16037 | +3975 (+32.95%) |
| API requests | 10 | 18 | +8 (+80.00%) |
| mean duration | 9.319s | 10.628s | +1.309s (+14.05%) |

## 6. Per-case detail

### A_single_agent

| case | decision | expected | decision correct | findings | eval passed | eval reason | TP | FP | FN | tokens |
|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| bm-01 | APPROVE | APPROVE | yes | 4 | no | clean_case_fp=4 | 0 | 4 | 0 | 1023 |
| bm-02 | REJECT | HOLD | no | 4 | no | decision=REJECT_expected=HOLD | 1 | 3 | 0 | 1043 |
| bm-03 | REJECT | HOLD | no | 3 | no | decision=REJECT_expected=HOLD | 2 | 1 | 0 | 478 |
| bm-04 | REJECT | HOLD | no | 4 | no | decision=REJECT_expected=HOLD | 3 | 1 | 0 | 700 |
| bm-05 | REJECT | HOLD | no | 3 | no | decision=REJECT_expected=HOLD | 1 | 2 | 3 | 1297 |
| bm-06 | HOLD | HOLD | yes | 1 | yes | all_matched | 1 | 0 | 0 | 702 |
| bm-07 | REJECT | REJECT | yes | 3 | no | fn=1 | 0 | 3 | 1 | 1159 |
| bm-08 | APPROVE | APPROVE | yes | 2 | no | clean_case_fp=2 | 0 | 2 | 0 | 1192 |
| bm-09 | REJECT | HOLD | no | 4 | no | decision=REJECT_expected=HOLD | 1 | 3 | 1 | 1736 |
| bm-10 | REJECT | REJECT | yes | 5 | yes | all_matched | 3 | 2 | 0 | 2732 |

### B_mergepilot

| case | decision | expected | decision correct | findings | eval passed | eval reason | TP | FP | FN | tokens |
|---|---|---|---|---:|---|---|---:|---:|---:|---:|
| bm-01 | APPROVE | APPROVE | yes | 0 | yes | clean_approved | 0 | 0 | 0 | 432 |
| bm-02 | REJECT | HOLD | no | 2 | no | decision=REJECT_expected=HOLD | 1 | 1 | 0 | 1744 |
| bm-03 | REJECT | HOLD | no | 3 | no | decision=REJECT_expected=HOLD | 2 | 1 | 0 | 1337 |
| bm-04 | REJECT | HOLD | no | 3 | no | decision=REJECT_expected=HOLD | 3 | 0 | 0 | 1738 |
| bm-05 | REJECT | HOLD | no | 4 | no | decision=REJECT_expected=HOLD | 1 | 3 | 3 | 2474 |
| bm-06 | HOLD | HOLD | yes | 1 | yes | all_matched | 1 | 0 | 0 | 1224 |
| bm-07 | REJECT | REJECT | yes | 1 | no | fn=1 | 0 | 1 | 1 | 1125 |
| bm-08 | APPROVE | APPROVE | yes | 0 | yes | clean_approved | 0 | 0 | 0 | 526 |
| bm-09 | REJECT | HOLD | no | 3 | no | decision=REJECT_expected=HOLD | 1 | 2 | 1 | 2780 |
| bm-10 | HOLD | REJECT | no | 4 | no | decision=HOLD_expected=REJECT | 3 | 1 | 0 | 2657 |

## 7. Secret-pattern scan

- synthetic_fixture_pattern_hits: **2** (raw-run files that contain a substring byte-for-byte identical to a credential-like value present in a benchmark fixture)
- real_credential_hits: **0**
- matched files: 2
  - `bm-02-B_mergepilot-7631f0.json` classification=`synthetic_fixture` synthetic_match_count=1 real_indicators=[]
  - `bm-08-A_single_agent-ac67e5.json` classification=`synthetic_fixture` synthetic_match_count=1 real_indicators=[]

_Matched values are compared byte-for-byte against credential-like substrings present in benchmark/dataset/fixtures/*.py. Only sha256 prefixes of matched values are recorded; full string contents are never emitted._

## 8. Formal conclusion

在 N=10、同模型、每个 pair 单次运行的受控本地评测中，MergePilot-style 多角色编排相较单 Agent 将 precision 从 36.36% 提升至 57.14%，F1 从 48.00% 提升至 63.16%，recall 同为 70.59%。改善主要来自 FP 从 21 降至 9；代价是 token 增加 32.95%、API 请求增加 80%。B 的 decision accuracy 为 40%，低于 A 的 50%，风险处置校准仍需改进。

## 9. Limitations

- N=10, small sample.
- Each (case x group) pair is run exactly once; no per-pair variance estimate.
- Single model: deepseek-v4-flash; no cross-model comparison.
- Synthetic fixtures hand-authored for the benchmark; not representative of the distribution of real-world PRs.
- Controlled local orchestration: Group B is NOT a real Gateway/controller/GitHub/HiClaw end-to-end run; it exercises the same deepseek model with a reviewer→fixer handoff simulation, not the production control plane.
- Does not support metrics that require real fix/verify/rollback execution (fix first-pass rate, rollback success rate, etc.); those are intentionally excluded rather than self-reported.
- Does not prove multi-role orchestration improves recall; recall is identical (70.59%) across both groups.
- C3 10/10 is independent real isolated-stack evidence (MergePilot-Test isolated dockerd + real Gateway/GitHub MCP/fixture repo) and MUST NOT be conflated with this benchmark; it is reported separately.
- N>=10 minimum target met; N>=20 remains a follow-up target.
- hiclab_live=false: this benchmark does not exercise the production HiClaw runtime.

---

_This file is machine-generated by `benchmark/freeze_formal_results.py`. Do not edit by hand; re-run the freeze to reproduce._
