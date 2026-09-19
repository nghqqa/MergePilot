gh-pr3-review-1 / run-gh-pr3-9fd86556-053436 - Independent security review of PR #3 (nghqqa/fastapi-boilerplate-demo, webhook-triggered).
1) taskflow(ack_task) taskId "gh-pr3-review-1".
2) Workspace setup (self-contained; no pre-seeded metadata):
   cd ~ && git clone --quiet https://github.com/nghqqa/fastapi-boilerplate-demo.git ghwork-run-gh-pr3-9fd86556-053436
   cd ~/ghwork-run-gh-pr3-9fd86556-053436 && git checkout --quiet 9fd8655633ba788eee17871c73c43e0bb7ea6b11 && git rev-parse HEAD (MUST equal 9fd8655633ba788eee17871c73c43e0bb7ea6b11; else stop and report BLOCKED)
   git diff --stat $(git merge-base 4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c 9fd8655633ba788eee17871c73c43e0bb7ea6b11)..9fd8655633ba788eee17871c73c43e0bb7ea6b11  # this IS the PR change set (merge-base 免疫 base 分支漂移); review the changed files only
3) Independent review: read changed code; if you suspect a vulnerability, write and run your own PoC against the checked-out tree; run the PR's own tests if present. Deterministic skills available via MCP (skill_diff_parse / skill_risk_classify / skill_sast_scan / skill_case_retrieval - advisory only, never replace your own judgment). rag_retrieve provides org standards (references only).
4) taskflow(submit_task) with YOUR independent conclusion, including exactly: STATUS: FINDING_CONFIRMED|NOT_CONFIRMED; SEVERITY: HIGH|MEDIUM|LOW; HUMAN_VERIFICATION_REQUIRED: YES|NO; plus evidence (PoC outputs / file:line).
5) Reply in team room: TASK_COMPLETED: run-gh-pr3-9fd86556-053436-review
Constraints: no repo modification; zero GitHub writes; your own workspace only.