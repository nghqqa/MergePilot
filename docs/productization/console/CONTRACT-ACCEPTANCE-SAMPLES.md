# 契约 v2 验收样例（CONTRACT-ACCEPTANCE-SAMPLES）

**目的**：给后端会话一组可直接复现的请求/响应样例——实现完成后按此逐条验收，前端适配层（`console/frontend/src/api-live.js`）即按这些形状消费。
**契约依据**：`console/API-AUTH-MERGE-V0.md` v2（设计分支 `docs/architecture-audit-20260922` @ **7ccecb9**，接受记录 c664df2）。
**状态（2026-09-23）**：以下 1–5 后端**均未实现**——前端已完成适配层、契约测试（`console/backend/test/live-api.test.mjs`）与明确标注的 fixture；第 6 节为**已联调**的真实只读接口（console_v3）。

## 1. 会话：GET /api/auth/session

未登录（前端据此显示登录页，不重定向）：
```
GET /api/auth/session
→ 401
{"error":{"code":401,"reason":"not_authenticated","message":"未登录"}}
```
会话过期（前端区分"请重新登录"）：`reason:"session_expired"`；已登录：
```
→ 200
{"user":{"user_id":"u-1","github_login":"ngh","display_name":"NGH","role":"operator"},
 "expires_at":"2026-09-23T18:00:00Z","capabilities_version":1}
```
前端消费：`api-live.fetchSession()` → authed/anonymous/expired/forbidden(not_a_member)/auth_unavailable(503)/not_implemented(404)/unavailable(网络)。

## 2. 能力：GET /api/me/capabilities?repo=owner%2Fname

```
GET /api/me/capabilities?repo=acme%2Fwidget
→ 200
{"repo":"acme/widget","installation_state":"both","operations":{
  "approve_tickets":{"allowed":true},
  "request_merge":{"allowed":false,"reason":"merge_disabled",
    "detail":"站内合并未启用——使用 GitHub 原生合并","github_url":"https://github.com/acme/widget/pull/9"}}}
```
未登录时同端点 → 401（无豁免）。前端按 allowed/reason 渲染，不显示永久"开发中"按钮。

## 3. PR 列表：GET /api/pulls?repo=owner%2Fname

```
GET /api/pulls?repo=acme%2Fwidget&state=open&limit=20&offset=0
→ 200
{"data_mode":"live","total":1,"items":[{
  "repo":"acme/widget","pr_number":9,"title":"…","state":"open",
  "current_head_sha":"<40hex GitHub 实时>",
  "latest_run":{"run_id":"…","status":"RUNNING","class":"execution","mode":"on","started_at":"…"},
  "latest_result":{"run_id":"…","head_sha":"<40hex 旧>","stale":true,
     "verdict":"FINDING_CONFIRMED","severity":"HIGH",
     "published":{"check_run_id":1,"conclusion":"action_required"}},
  "has_pending_tickets":1}]}
```
前端消费：`stale=true` → 显式"旧 head 结论"标注；`latest_run.status=RUNNING` 且无完成结果 → "当前 head 审查进行中"（不用旧结论顶替）。

## 4. PR 详情：GET /api/pulls/:prNumber?repo=owner%2Fname

```
GET /api/pulls/9?repo=acme%2Fwidget
→ 200
{"repo":"acme/widget","pr_number":9,"title":"…","state":"open","current_head_sha":"<40hex>",
 "runs":[{"run_id":"…","chain":"…","class":"execution","exec_seq":1,"mode":"on",
          "status":"…","outcome":"…","head_sha":"…","stale":false,"created_at":"…"}],
 "merge_panel":{"enabled":false,"methods":[],"github_url":"https://github.com/acme/widget/pull/9",
   "reasons":["merge_disabled"]},
 "patch_delivery":{"download_url":"…","applied_to_pr":false,
   "note":"下载≠已应用——是否应用到 PR 由 GitHub 记录为准"}}
```

## 5. 站内合并（默认关闭）：POST /api/repos/:repoId/merge-requests

```
POST /api/repos/:repoId/merge-requests?repo=acme%2Fwidget
X-CSRF-Token: <会话下发的 csrf>
{"pr_number":9,"merge_method":"squash"}
→ 403 {"error":{"code":403,"reason":"merge_disabled","message":"站内合并未启用"}}
```
前端行为：合并保持关闭，PR 详情仅提供 GitHub 外链；写请求一律携带 `X-CSRF-Token`（含 PATCH）。

## 6. 已联调：console_v3 只读接口（当前唯一真实后端 HTTP 查询）

```
GET http://127.0.0.1:4191/healthz → 200 {"ok": true, "mode": "read-only"}
GET http://127.0.0.1:4191/api/runs → 200 {"runs":[{run_id,mode(shadow|fixture|on),repo,pr_number,
     head_sha,risk_tier,outcome,review_complete,coverage_missing,superseded,updated_at}],
     "data_mode":"shadow/fixture only"}
GET /api/runs/NO-SUCH-RUN → 404；POST /api/runs → 405（GET-only）
```
隔离联调记录：`console/verification/console-contract-alignment/v3-itest.log`（环境门控测试
`console/backend/test/live-v3.integration.test.mjs`，2026-09-23 对隔离 fixture 库实例实测通过：
列表→适配聚合→详情→404→405）。数据边界：console_v3 数据为 shadow/fixture 模式产物，
展示时保留 mode 标签，**不构成真实运行/真实审查结论**。
