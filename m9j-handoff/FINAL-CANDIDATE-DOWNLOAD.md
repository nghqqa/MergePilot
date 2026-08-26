# M9-J Final Candidate Download — v0.1.0-preview.4 (merged main)

Release: https://github.com/nghqqa/MergePilot/releases/tag/m9j-preview4-final-candidate

**NOT the formal v0.1.0-preview.4.** This is the merged-main final candidate
for byte-level acceptance. The formal tag is created ONLY after this
candidate passes the full lifecycle regression on the original test machine.

## Identity

| Field | Value |
|---|---|
| version | `v0.1.0-preview.4` |
| git_commit | `5bb2635624585318aa005a0fc1d8c2b302c8a183` |
| lineage | PR #209 merge `dac9fca` ← RC.5 branch `9cf32dc` (23 commits) → release-prep `1d6f7f5` → header fix `5bb2635` |
| dev gate | 2471 passed / 0 failed / 20 skipped (re-run live at merged main) |
| images | 9/9 identity-identical to RC.5 (recomputed from shipped tar) |

## Assets (7)

| File | Bytes | SHA-256 (GitHub digest) |
|---|---|---|
| mergepilot-v0.1.0-preview.4-package.zip | 868978 | a59699fbfbf7471544f47e5a3851322452e90637bb15ab7997009a22766b3e07 |
| images-oci.tar | 847388160 | 74869211270c01d97ba9633738e2bb60ecbb1e1aa696fe9f0e4631ad0772ed27 |
| manifest.json | 18687 | 90f499f206693a26ad1d10c5744224c34f800c5238573b0744e44521acfa2ef4 |
| checksums.sha256 | 267 | 1e5a02c319be5110fa0fc0fbbff77b04911a6656db91919cad66e37371840bed |
| SHA256SUMS | 22636 | 60168f17341019ad9b036308fe3e907004336ca500ad98f4518e7883385a6bfa |
| EXTERNAL-ACCEPTANCE.md | 1366 | 7822c3e056de451c5826c0a4d910886ae90fe34303527bfa13d702c1d6359fc3 |
| asset-sha256.json | 32374 | 75344d659ce267c6b316ab67130060e5b0349f34fcc95943daa9479f275a5cb3 |

## Dev-machine verification already done (2026-08-26)

Check → Install → Doctor OK(ok) → Status absent → Start (healthy,
page/api 200, 405/404/403) → Stop → re-Start (healthy) →
Cleanup #1 (manifest-consumed report) → Cleanup #2 (absent-from-start
report) → zero residue. Download-verified: GitHub digest 7/7,
checksums 3/3, SHA256SUMS 217, asset-sha256 217/217, ZIP 213 entries
0 cache, OCI blobs 182/182 + config-digest 9/9 on the redownloaded tar.

## What the test machine must do

Re-download all 7 assets from the release URL above and run the full
regression per `rerun-command.txt`. Verify the Cleanup contract lines
exactly (see `cleanup-contract.json`).
