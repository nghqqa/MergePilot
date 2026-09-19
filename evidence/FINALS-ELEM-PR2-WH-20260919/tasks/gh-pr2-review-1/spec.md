gh-pr2-review-1 / run-gh-pr2-1414edbe-052650 - Independent security review of PR #2 (nghqqa/fastapi-boilerplate-demo, webhook-triggered).
1) taskflow(ack_task) taskId "gh-pr2-review-1".
2) Workspace setup (self-contained; no pre-seeded metadata):
   cd ~ && git clone --quiet https://github.com/nghqqa/fastapi-boilerplate-demo.git ghwork-run-gh-pr2-1414edbe-052650
   cd ~/ghwork-run-gh-pr2-1414edbe-052650 && git checkout --quiet 1414edbe513620262b31372ed1af4027be5888d1 && git rev-parse HEAD (MUST equal 1414edbe513620262b31372ed1af4027be5888d1; else stop and report BLOCKED)
   git diff --stat $(git merge-base 4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c 1414edbe513620262b31372ed1af4027be5888d1)..1414edbe513620262b31372ed1af4027be5888d1  # this IS the PR change set (merge-base 免疫 base 分支漂移); review the changed files only
3) Independent review: read changed code; if you suspect a vulnerability, write and run your own PoC against the checked-out tree; run the PR's own tests if present. Deterministic skills available via MCP (skill_diff_parse / skill_risk_classify / skill_sast_scan / skill_case_retrieval - advisory only, never replace your own judgment). rag_retrieve provides org standards (references only).
4) taskflow(submit_task) with YOUR independent conclusion, including exactly: STATUS: FINDING_CONFIRMED|NOT_CONFIRMED; SEVERITY: HIGH|MEDIUM|LOW; HUMAN_VERIFICATION_REQUIRED: YES|NO; plus evidence (PoC outputs / file:line).
5) Reply in team room: TASK_COMPLETED: run-gh-pr2-1414edbe-052650-review
Constraints: no repo modification; zero GitHub writes; your own workspace only.