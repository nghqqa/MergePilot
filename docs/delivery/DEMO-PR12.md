# 演示脚本：从缺陷 PR 到确认合并（15 分钟，复用真实证据）

> 本脚本把 **github.com/nghqqa/test-repo 的 PR#12**（2026-10-15 完成的真实全链）
> 串成一场约 15 分钟的演示。**演示全程零新写入**：不重新审批、不重新发布评论、
> 不重新应用补丁、不重新合并——所有关键节点展示的都是当日已完成的历史事实，
> 以「只读查看」方式呈现（GitHub 公开页面 + Console 历史记录）。
> 演示中如需展示交互动作，只能在演示实例的**练习仓库另开新 PR** 完成，并明确向
> 观众声明那是新的真实写入；不要对 PR#12 及其分支重复任何远端操作。

## 0. 演示前置

| 项 | 内容 |
|---|---|
| 外部页面 | [PR#12](https://github.com/nghqqa/test-repo/pull/12)（仓库为 public，可直接打开） |
| Console | 部署方提供地址与**只读查看账号**（contributor 即可看 run/发现/产物；审计需 auditor） |
| 讲述口径 | 每一幕先说「这一步是历史事实（哪天完成）」还是「现场只读查看」 |

## 1. 分幕脚本

### 幕 0 · 定位（60 秒）

MergePilot 是自托管 PR 审查工作台。原则一句话：**系统不自动改代码、不自动合并；
每一步远端写（评论/机器人提交/合并）都要维护者逐次「预览 → 确认」**。
今天的链路：一个含真实缺陷的 PR，从自动审查到维护者确认合并，全程留痕。

### 幕 1 · 缺陷进入，真实 CI 变红（2 分钟）【历史证据】

- PR#12「折扣助手：含 null cart 崩溃缺陷 + 判别力测试」，3 个文件：
  `discount.js`（注入 null 购物车即崩溃）、`tests/discount.test.js`（判别力测试）、
  `suggested.patch`。
- 打开 GitHub Actions run [38685928675](https://github.com/nghqqa/test-repo/actions/runs/38685928675)：
  **红**——判别力测试真实抓到了崩溃（不是摆拍数据，是仓库自己的 CI）。
- 讲点：审查对象是真实可复现的缺陷，验证手段是真实 CI。

### 幕 2 · 自动审查与发现（2 分钟）【Console 只读查看】

- webhook 自动触发审查。Console「运行记录」→ run `33d11526`（最终轮）：
  发现列表含 **P1 逻辑缺陷**（`source=reviewer`，模型发现）与静态规则发现，来源分列。
- 讲点：静态规则与模型审查解耦；模型发现独立标注、绝不冒充「已执行」。

### 幕 3 · 审批门：没有批准就没有修复（1.5 分钟）【Console 只读查看】

- 「审批」页：run `33d11526` 的 P1 具名票 `bea5d7b4`（带 finding/位置/head/有效期）。
- 审计时间线：07:16:53 `FIX_APPROVAL_REQUESTED` → 07:16 稍早 `FIX_BLOCKED_NO_APPROVAL`
  （**未批准时修复轮被 fail-closed 拒绝**）→ 07:18:51 `FIX_APPROVED`（维护者）→
  07:19:03 `FIX_STARTED_AFTER_APPROVAL`。
- 讲点：高危修复的生成也卡在人工批准之后；驳回/过期/STALE 均为终态，永不自动恢复。

### 幕 4 · 补丁与验证三分（2 分钟）【Console 只读查看】

- run 详情产物面板：建议补丁（统一 diff，版本化 v1，digest `f8c7fbe2…`）；下载的
  `.patch` 文件头含同一 digest。
- 验证三分：格式与路径检查通过；**真实可应用性 = 通过**（对准确 head 实测
  `git apply --check`，审计 `at_patch_apply_check ok=true`）；实际修复测试取决于部署
  是否配置 `MU_FIX_TEST_CMDS`（未配置时如实显示「未执行」）。
- 讲点：**可应用 ≠ 修复正确**；修复正确性靠修复前/后测试证据与本 PR 自带判别力测试
  在幕 7 的 CI 结论。

### 幕 5 · 预览并确认应用 → 机器人提交（2 分钟）【历史证据 + Console 只读查看】

- maintainer 在「应用修复与合并」区点「① 预览并应用修复」→ 预览（显示远端 head、
  补丁 digest、写入身份）→ 确认。
- 结果：机器人以 **MergePilot Bot** 身份向 `fix/null-cart-guard` 推送修复提交
  [be8789d6](https://github.com/nghqqa/test-repo/commit/be8789d6cf2cd851ebaddf2885ae0822d7d46d95)
  （提交信息注明来源 run 与补丁版本）；操作记录 `bf19706e` 状态 `committed`，
  审计 `MU_FIX_APPLIED`（07:39:13）。
- 讲点：推送式提交（非快进被远端拒绝）；确认绑定 head+digest，预览后换版会被拒；
  重复确认幂等。若推送结果不明 → `result_uncertain`，必须「读回对账」，禁止盲目重试。

### 幕 6 · 新 head 自动重审（1.5 分钟）【Console 只读查看】

- 应用成功即自动入队重审（审计 `MU_REVIEW_REENQUEUED_AFTER_APPLY`，job `db42e3be`）：
  新 head be8789d6 的新 run `6d7d3267` 状态 **REVIEWED**——P2×2 + P3×3，**无 P0/P1**。
- 讲点：**旧 head 的批准与验证不能证明新 head 合格**——新 head 重新积累全部证据。

### 幕 7 · 新 head 的真实 CI 转绿（1 分钟）【历史证据】

- GitHub Actions run [38700203253](https://github.com/nghqqa/test-repo/actions/runs/38700203253)：
  **绿**（同一判别力测试，be8789d6 上通过）。
- 讲点：审查完成 ≠ CI 通过；合并资格门两者都要，且只认**当前 head** 的 CI。

### 幕 8 · 资格门与确认合并（2 分钟）【历史证据 + Console 只读查看】

- 合并预览显示资格门四条件：PR 开放+head 一致 / mergeable 已知 / 当前 head 审查完成
  无未处理 P0/P1 / 当前 head 真实 CI 通过——全部满足才放行（preview 与 confirm 同一
  判定，confirm 时重读远端）。
- maintainer「② 预览并合并 PR」→「确认合并 PR」：merge commit
  [e34f1d45](https://github.com/nghqqa/test-repo/commit/e34f1d45e8f199b431c441100ddbbbef57774281)
  （"Merge pull request #12 (via MergePilot)"）；操作记录 `3eea39df` 状态 `merged`，
  审计 `MU_PR_MERGED`（07:41:49）；合并后 main 的 CI 亦绿（run 38700491427）。
- 讲点：平台自身分支保护照常生效、不被绕过；已合并后重复确认幂等返回。

### 幕 9 · 审计链复盘（1 分钟）【Console 只读查看】

审计页按时间读出本次全链事件序列：
`REVIEW_LLM_EGRESS` → `FIX_APPROVAL_REQUESTED` → `FIX_BLOCKED_NO_APPROVAL` →
`FIX_APPROVED` → `FIX_STARTED_AFTER_APPROVAL` → `at_fix_patch_persisted` →
`at_patch_apply_check` → `agentteams_round_completed` →
`MU_REVIEW_REENQUEUED_AFTER_APPLY` → `MU_FIX_APPLIED` → `MU_PR_MERGED`。

讲点：每个关键动作一个审计事件，谁、何时、对哪个对象——事后可对账、可回溯。

## 2. 证据索引（均为 2026-10-15 历史事实，只读引用）

| 节点 | 稳定标识 |
|---|---|
| 缺陷 PR | nghqqa/test-repo#12，初始 head `17eb44ad…`，CI 红 run `38685928675` |
| 审查 run / 票 / 补丁 | run `33d11526-f982-4d29-b464-05e6e22fb239`；票 `bea5d7b4`（P1）；补丁 `719f6665`（v1，digest `f8c7fbe2…`） |
| 机器人提交 | `be8789d6cf2cd851ebaddf2885ae0822d7d46d95`（apply 操作 `bf19706e` committed） |
| 重审 run | `6d7d3267-568f-4237-80c1-8e5dc0baeffd`（REVIEWED，入队 job `db42e3be`） |
| 新 head CI | run `38700203253`（绿） |
| 合并 | merge commit `e34f1d45e8f199b431c441100ddbbbef57774281`（merge 操作 `3eea39df` merged；main CI run `38700491427` 绿） |
| 版本 | rc.35.5（源码 af8b0022，镜像 `sha256:e3041999…61dd3a`） |

## 3. 叙事勘误（讲述时以此为准）

- run `33d11526` 对应的批准票是 `bea5d7b4`（`a9d602b5` 属上一轮 run `c7e3df15`）——
  部分早期记录把两者混写，以库内审计为准。
- 「P1×1 P2×2 P3×2」是旧 head 页面快照口径；新 head 重审 run `6d7d3267` 的权威结论
  是 P2×2 + P3×3、无 P0/P1。

## 4. 截图（待补）

以下截图需取自真实页面并完成脱敏（隐去账号/组织/租户标识）后补入本目录，**在补入前
本脚本不使用任何替代示意图**：

- [ ] PR 详情「应用修复与合并」区块（含「读回对账」入口与历史操作列表）
- [ ] 合并预览资格门四条件
- [ ] run 详情发现列表（`precheck` / `reviewer` 来源分列）
- [ ] 审批页具名票
- [ ] 审计页 `MU_FIX_APPLIED` / `MU_PR_MERGED` 事件行

候选来源（内部评审留档，未脱敏，勿直接外发）：`r3work/frontend-ux-review-20261015/screenshots/`
（PR#12 详情页 7 张实机截图）。
