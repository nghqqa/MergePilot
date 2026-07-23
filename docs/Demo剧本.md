# MergePilot · Demo 剧本(对应方案 PPT 的 S13)

> 一份可复现的现场演示流程。用 fixture PR(确定性输入),不怕 LLM 抖动。
> 预计演示时长:5–8 分钟。

---

## 演示前准备(确认)

- [ ] HiClaw 在跑,Team mergepilot Active 3/3(`docs\原型搭建-Team重建.md` 的 runbook)
- [ ] Element Web 登录 admin,能 DM coordinator
- [ ] `python tools/trace_aggregator.py` 可用(演示完生成 trace)

---

## 第一幕:高危 PR 被拦下(主戏)

**讲述**:「这是一条普通的功能 PR,但里面藏了两个高危问题。看 MergePilot 怎么处理。」

1. 用**可靠触发器**(走 Manager 路由)提交 fixture(pr-01):
   ```bash
   MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp /mnt/d/goai/tools/submit_pr_manager.py hiclaw-manager:/tmp/ && docker cp "/mnt/d/goai/fixtures/pr-01-密钥与SQLi.md" hiclaw-manager:/tmp/pr.md && docker exec hiclaw-manager python3 /tmp/submit_pr_manager.py /tmp/pr.md <admin_password>'
   ```
   > **必须走 Manager**(系统 Manager,openclaw)路由——直戳 coordinator(copaw)的 DM 不可靠(已实测)。Manager 接收 → 建 task → 路由给 mergepilot team → coordinator 自动拆 review→fix→verify。
2. (或手动:Element 里给 `manager` 发"请让 mergepilot team 处理这个 PR:[代码]";**不要**直接 DM coordinator。)
3. 边等边讲:「coordinator 现在自动把任务拆成 review→fix→verify 三个子任务,分派给三个 specialist。」
4. **看点**(在各 agent 房间 + coordinator 房间):
   - reviewer 抓到 F-001(密钥,L2)、F-002(SQLi,L2)、F-003(泄漏,L0)
   - fixer 修了三个,但对 L2 标 **needs-approval**——**不自动合并**
   - verifier 验证通过,但因 L2 → 整体裁定 **hold**
5. **亮相比**:「注意——高危变更它**没敢自动合并**,挂了审批门等人。这就是和普通 PR Bot 的本质区别。」
6. coordinator 会出审批待办 → 你回复「作为 Team Admin,批准 F-001/F-002 部署」→ fixer 应用生产补丁 → verifier 最终复验(40 项检查)→ **merge**。

---

## 第二幕:依赖漏洞(供应链维度)

**讲述**:「不只是代码层,依赖层的安全它也管。」

1. 提交 `fixtures/pr-02-依赖漏洞.md`。
2. 看点:reviewer 识别出降级到有 CVE 的 `cryptography==37.0.0`,标 L2 → hold。
3. 这条演示**多维度**(代码漏洞 vs 供应链),对应附录B 的 DepVulnCheck 类 Skill。

---

## 第三幕:干净 PR 放行(负例,反差)

**讲述**:「它也不会为了凑数而误报。干净 PR 直接放行。」

1. 提交 `fixtures/pr-03-干净PR.md`。
2. 看点:NO FINDINGS → 裁定 **merge**(自动合并)。
3. 反差收尾:和第一幕的 hold 形成对比,证明**风险分级是真的在判**。

---

## 收尾:展示执行证据

1. 跑 `python tools/trace_aggregator.py`,打开 `evidence/<project>/trace.md` 展示 **DAG 时间线**(reviewer→fixer→verifier,标出哪步 needs-approval)。
2. 打开 `evidence/<project>-05/result.md`(最终验证报告)展示 **40 项检查全过、AST 分析、SQLi 注入测试**。
3. 一句话总结:「扔 PR 进去 → 自动跑完审查修复验证 → 高危管住等审批 → 全程有 trace、有证据、可审计。」

---

## 应急(演示翻车时)

- **agent 卡住不回**:重启容器(`docker restart hiclaw-worker-coordinator`),在原房间重发。
- **想快速过流程**:直接用 PR-01,它的 5 节点 DAG 是最完整的展示,跑通就够说明问题。
- **现场没网/DeepSeek 抖**:fixture 是确定性的,重跑结果稳定;实在不行,用已生成的 `evidence/` 和 `trace.md` 静态展示。
