# MergePilot 新用户验收清单（rc.35.4 交付口径）

> 逐项打勾验收。每项给出**操作 → 预期结果**；「已验证」只认你亲自跑出的结果。
> 第 8/9 步能力已在 main（PR #455-#457）实现、随 rc.35.5 发布生效；真实应用与合并保留用户逐步确认节点。
> 排查：任何一步不符 → [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 对应条目。

## 前置

- [ ] 部署完成（[DEPLOY-GUIDE.md](DEPLOY-GUIDE.md) 目标一）；console 地址、首个平台管理员账号在手。
- [ ] 诊断工具全绿：
      `node scripts/delivery/diagnose.mjs --env deploy/selfhost/.env --base-url <地址> --pg-container <pg容器>` 退出码 0。

## 1. 登录

- [ ] 管理员按受邀人 **GitHub/Gitee 数字 id** 创建邀请，受邀人从邀请链接登录成功。
- [ ] 顶栏显示「身份 · 组织 · 角色」，与邀请授予的角色一致。
- [ ] 反向验证：未被邀请的账号登录被拒（`not_invited`）。

## 2. 身份确认（权限语义）

- [ ] 用非维护者账号（如 reviewer）登录：审批/发布按钮不可用，且给出原因（无 decide_review）。
- [ ] 用 platform_admin 账号登录：成员/设置可管理，但**审批/发布同样不可用**（平台管理与裁定分离）。
- [ ] 用 maintainer 账号登录：审批/发布入口可用。

## 3. 仓库接入

- [ ] maintainer 完成 GitHub App 安装并绑定仓库（或 Gitee 连接绑定）。
- [ ] 仓库页显示绑定状态 `active`。
- [ ] 反向验证：未绑定仓库的 PR 不产生 run。

## 4. 审查触发与发现

- [ ] 向绑定仓库开测试 PR → 约 1 分钟内「运行记录」出现新 run，状态推进到 `REVIEWED`。
- [ ] 发现列表可见，来源标注正确（静态规则 `precheck`；启用 LLM 后另有 `reviewer`）。
- [ ] finding 反馈（确认/误报/暂不处理）可提交且留痕。

## 5. 理解验证结果

- [ ] run 详情能看到建议补丁（统一 diff）与验证三分区块。
- [ ] 「真实可应用性」为五态之一且与实际一致（未配置环境时如实显示未验证，不显示通过）。
- [ ] 「实际修复测试」：未配置 `MU_FIX_TEST_CMDS` 时如实显示未执行；配置后显示 pre/post 退出码。
- [ ] 下载的 `.patch` 文件头含 digest，与页面显示的 active 版本一致。

## 6. 审批修复（maintainer）

- [ ] 制造一个 P0/P1 缺陷的 PR → 审查后「审批」页出现逐条具名票。
- [ ] 全部 P0/P1 票批准后修复轮启动；产物为建议补丁 + 验证证据（dry-run，**远端无任何新提交**）。
- [ ] 反向验证：任一票驳回 → run BLOCKED；过期/STALE 票终态后不自动恢复。

## 7. 发布审查评论（maintainer）

- [ ] 「预览并发布」显示：目标平台/PR、审查 head、远端当前 head、写入身份（机器人）、评论全文。
- [ ] 确认后 PR 页出现机器人评论；内容与面板产物一致（含验证三分表述）。
- [ ] 同 run 再次发布 → 原位更新同一评论（不刷屏）。
- [ ] PR 推进新 head 后对旧 run 发布 → 被拒（409 `head_moved`）。

## 8. 应用修复 → 新审查与 CI 【rc.35.5 收口语义（#458+可靠性收口）】

- [ ] 预览应用（GET preview）→ 确认应用（POST confirm，**必带** `expected_remote_head` + `expected_patch_digest`；缺任一字段 400 `invalid_confirm_fields`）→ 机器人向 PR 源分支新增修复 commit（推送式，非快进被服务端拒绝），操作记录（mu.fix_apply_operation）先落 confirmed/executing 并在推送前持久化候选 commit SHA。
- [ ] 预览后补丁被修正闭环换版 → 确认被拒（409 `patch_changed`——禁止静默应用新版本）；存储补丁字节与登记 digest 不符 → `digest_mismatch` 拒绝。
- [ ] 并发双确认 → 数据库原子认领单赢家；败方 409 `operation_in_progress` 且零覆写胜方记录。
- [ ] 推送超时/断连/响应丢失 → `result_uncertain`（候选 SHA 已登记），前端出现「读回对账」入口；**直接重试确认被拒**（`result_uncertain_requires_reconcile`）；对账按远端实际定案（已落地→committed 并自动补齐重审入队；未落地→failed 可安全重试；证据不足保持 uncertain）。
- [ ] 重复确认 → 幂等返回既有结果（幂等查询先于远端检查——push 落地后 head 已推进仍成立）；已应用后预览报 `already_applied`。
- [ ] 应用成功 → 自动按新 commit SHA 入队重审（与 webhook 同一 event_sync 消费单元；同 head 已有 run/job 即去重；入队失败可恢复且 push 不回退）。旧 head 的批准/验证结果不替代新 head 证据。

## 9. 确认合并 【rc.35.5 收口语义（统一资格门）】

- [ ] 合并预览与确认使用同一资格判定（confirm 时重读远端）：PR 开放 + head 与预览一致（confirm 必带 `expected_head`）+ mergeable 已知且可合并（**null 不放行**）+ **当前 head 存在审查 run 且状态为 REVIEWED/COMPLETED/VERIFIED**（REVIEWED=干净 PR 合法终态；旧 head 证据不可替代）+ 当前 head 无未处理 P0/P1 + **当前 head 真实 CI 通过**。
- [ ] CI 判定：GitHub 读 combined status **与** check runs 双面（Actions 仓库只有 check runs 也覆盖）；任一失败=阻塞、任一进行中=阻塞、两面全空=**not_configured 阻塞**（不冒充通过）；403=unsupported 并报告所需权限（不用 PAT 绕过）。Gitee 现连接器无 CI 状态 API（双形态 404）→ 恒 unsupported 阻塞（能力边界，如实）。
- [ ] 条件合并（sha 匹配）→ merge commit 留痕（审计 MU_PR_MERGED）；平台保护/审查门在合并时由平台强制（Gitee 实测"未通过设置的审查/测试"两次真实拒绝如实呈现，不绕过）。
- [ ] 合并响应丢失 → result_uncertain → 对账按 PR 实际 state 定案（merged→记录 merge commit）；已合并后重复确认 → 幂等返回（不被 pr_open 提前拒绝）。

## 收尾记录

- [ ] 把本清单的验证时间、环境（镜像 digest/版本字段）、逐项结果记入验收记录。
- [ ] 生产环境确认 `MU_ALLOW_FIXTURE_LOGIN` 未设置、`.env` 权限 600、诊断工具退出码 0。
