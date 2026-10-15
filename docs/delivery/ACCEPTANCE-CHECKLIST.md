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

## 8. 应用修复 → 新审查与 CI 【rc.35.5 起可用】

- [ ] 预览应用（GET preview）→ 确认应用（POST confirm，携带 expected_remote_head）→ 机器人向 PR 源分支新增修复 commit，产生幂等操作记录（mu.fix_apply_operation）。
- [ ] 重复确认 → 幂等返回既有结果（不重复远端写）；远端 head 已推进 → 409 stale_head/head_moved_since_preview。
- [ ] 应用后新审查：当前需在平台手动触发（自动关联与 webhook 幂等去重未实现——如实登记）；旧 head 的批准/验证结果不替代新 head 证据。

## 9. 确认合并 【rc.35.5 起可用】

- [ ] 合并预览（pr_open/mergeable/head 可见；CI 如实标注 not_configured）→ maintainer 确认合并（expected_head 双确认）→ 条件合并（sha 匹配）→ merge commit 留痕（审计 MU_PR_MERGED）。
- [ ] 平台保护规则不足时合并被远端拒绝（如 GitHub 保护门），系统如实呈现拒绝原因、不绕过。

## 收尾记录

- [ ] 把本清单的验证时间、环境（镜像 digest/版本字段）、逐项结果记入验收记录。
- [ ] 生产环境确认 `MU_ALLOW_FIXTURE_LOGIN` 未设置、`.env` 权限 600、诊断工具退出码 0。
