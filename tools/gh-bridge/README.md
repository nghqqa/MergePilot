# gh-bridge — Phase 3 lite 桥(webhook 交付 → AgentTeams → check-run 回写)

服务器(`webhook-ingress-runbook.md`)与本机 AgentTeams 栈之间的桥接器。P14 workflow-controller
的轻量替代:不跑九服务栈,复用决赛九轮验证过的机制(MinIO 播种/agt wake/Matrix kickoff/报告监听)。

## 链路
github_deliveries(PENDING) --SSH/CAS 认领--> 播种项目(meta.json+plan.md,mc pipe)
→ 唤醒 worker(agt worker wake)→ kickoff(Leader DM)→ Reviewer 独立审查
→ 监听 Leader 终报(强标记:状态报告/最终报告/已完成,>300 字符)
→ 以 result.md 为权威产物解析结论 → 服务器 mp-checks-reporter 容器以 App 身份 POST check-run
→ delivery=PROCESSED。

## 2026-09-19 实测
PR #5/#6/#7 三个真实 PR 全链路跑通(文档变更,NOT_CONFIRMED/LOW → success)。
热 worker 下端到端约 40–120 秒。check run 以 MergePilot-Reporter [bot] 署名
与 GitHub Actions 并排显示于 PR commit。

## 已知边界(如实)
- 判定解析以 result.md 权威内容为准(HVR YES/FINDING+HIGH → action_required,其余 success);
  人工门批准/拒绝路径未自动化——门决策仍属操作员,符合"系统不决策合并"设计。
- 脚本内 SERVER/房间 ID 等常量为部署现场值,迁移环境需改配置(后续版本抽成 env)。
- bridge 为单实例设计(CAS 认领防并发,但多实例会争抢 watch;跑一份即可)。
