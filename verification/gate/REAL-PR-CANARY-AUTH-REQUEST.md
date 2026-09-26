# REAL_PR_FIX_VERIFY_CANARY — 授权闸门报告

基线：afb7f1c（未回退） · 2026-09-26

## 一 · 候选 PR 状态

| PR | repo | head | run | stage | findings |
|---|---|---|---|---|---|
| tizhou#2 | nghqqa/tizhou | c443150958b3 | run-stage-tz-2 | PASSED | **0** |
| speaktype#426 | wookat/speaktype | dc425e12f683 | run-stage-st-426 | PASSED | **0** |

## 二 · Finding 检查结果

**两个真实 PR 均无可修复 finding。**

证据：
- sast_scan 回执：status=OK，integrity=OK（两个 PR 各 1 条，零 finding）
- diff_parse 回执：status=OK（两个 PR 各 1 条）
- pending 队列：0 项
- gate 决策：两个 run 均 PRODUCE（skill 层通过，无阻断）

根据指令第 3 条：**不得伪造 finding，如实报告。**

## 三 · 两种路径

### 路径 A：隔离 clone 修复（无 GitHub 写入）

在本地 clone 或 fixture 中注入已知 finding（如 CWE-22 路径穿越），
运行 Fixer→Verifier 链。**适合验证链路正确性，不触及真实 PR。**

| 步骤 | 内容 | GitHub 写入 |
|---|---|---|
| 1 | clone 目标 repo 到本地（`git clone` 只读） | 0 |
| 2 | 在本地工作区注入测试 finding | 0 |
| 3 | Fixer 生成 patch（realpath containment 修复） | 0 |
| 4 | patch digest + repo/head/run 绑定 | 0 |
| 5 | Verifier 独立验证（测试结果来自 harness） | 0 |
| 6 | 审计 + 回滚 + 工作区清理 | 0 |

**限制**：不能宣称真实 PR 已完成闭环。

### 路径 B：真实测试分支修复（需额外人工授权）

在 GitHub 上创建测试分支（含已知 finding），以 PR 形式运行完整链路。

| 步骤 | 内容 | GitHub 写入 | 需授权 |
|---|---|---|---|
| 1 | 创建测试分支 + commit（含 CWE-22 代码） | push | ✅ |
| 2 | 创建测试 PR | PR 创建 | ✅ |
| 3 | Review Agent 扫描 → finding | 0（只读） | — |
| 4 | Fixer 生成 patch | 0 | — |
| 5 | Verifier 验证 patch | 0 | — |
| 6 | patch commit + push 到测试分支 | push | ✅ |
| 7 | 更新 PR（注释/diff） | PR 更新 | ✅ |
| 8 | 人工审查 + 决定 | 0 | 人工 |

**不自动 merge、不自动 approve。**

## 四 · Fixer/Verifier 链路就绪状态

| 组件 | 状态 | 验证 |
|---|---|---|
| Fixer 代码模块 | ✅ READY | preflight 11/11 + staging 11/11 |
| Verifier 代码模块 | ✅ READY | preflight 7/7 |
| 联调（隔离 fixture） | ✅ READY | iso_chain 29/29 + canary 20/20 |
| patch digest 绑定 | ✅ READY | 漂移检测实测 |
| CAS fencing | ✅ READY | 并发竞争测试 |
| 审计 | ✅ READY | 五字段连续性 |
| 回滚 | ✅ READY | 工作区零残留 |

## 五 · 授权请求

### 选择路径
□ **路径 A**：隔离 clone（零 GitHub 写入）
□ **路径 B**：真实测试分支（需以下逐项授权）

### 路径 B 逐项授权（仅选择路径 B 时填写）

| # | 项目 | 允许？ |
|---|---|---|
| B1 | 目标 repo | □ wookat/speaktype □ nghqqa/tizhou |
| B2 | 测试分支名 | ＿＿＿＿ |
| B3 | 允许 commit 到测试分支 | □ 是 □ 否 |
| B4 | 允许 push 测试分支 | □ 是 □ 否 |
| B5 | 允许创建测试 PR | □ 是 □ 否 |
| B6 | 允许更新 PR（patch commit 后） | □ 是 □ 否 |
| B7 | 允许人工 approve | □ 是 □ 否 |
| B8 | 允许人工 merge | □ 是 □ 否 |
| B9 | 有效期 | ＿＿＿＿ |
| B10 | 回滚方式 | 删除测试分支 + 关闭 PR |

### 通用停止条件（沿用）
- stale 或无 receipt 产生 success
- patch/repo/head/run 绑定失败
- Verifier 未独立验证即产生 VERIFIED
- 真实 PR（#426/#2）被修改
- 审计缺失
- 回滚失败

---

**真实 PR 无可修复 finding——路径 A 可立即执行，路径 B 需逐项授权。**
**沉默、查看报告或模糊回复均不构成授权。**
