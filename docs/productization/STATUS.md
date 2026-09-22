# MergePilot 产品化推进状态（STATUS）

**更新**：2026-09-22（第二轮） ｜ **分支**：`chore/backfill-r3-ops`（连续提交 M1/M2 工作项）｜ 授权范围：M1→M4 至 V0 技术就绪

## 当前里程碑：M1 单测级收口（集成项待授权）＋ M2 审批规格启动

| 工作项 | 状态 | 说明 |
|---|---|---|
| M1-0 调用链与状态归属调研 | ✅ 完成 | 结论见 DECISIONS.md #1 |
| M1-0 验收矩阵 | ✅ 完成 | ACCEPTANCE.md |
| M1-1 发布语义加固（场景7/3/4/5/8 部分） | ✅ 已实现+已测试 | commit b46e8ba |
| M1-2 崩溃恢复与续接（场景1/2） | ✅ 已实现+已测试 | commit 0baa644 |
| M1-3 场景9 互斥契约 | ✅ 契约+单测 | ORCHESTRATION-CONTRACT.md；**§6 已补 lease_expires_at 客观边界核实（本轮）** |
| M1-3 场景6 PR 更新失效 | 🔒 结构保证+单测 | check-run 绑 head_sha + already_processed 按 head 去重；真实双 head 实证待授权案例轮 |
| M2-A 审批票据规格（四问） | ✅ 规格已定 | docs/productization/M2-APPROVAL-SPEC.md；merge 语义剥离；决策项 D-1/D-2/D-3 列明未拍板 |
| M2-A 绑定校验器+单测 | ✅ 已实现+已测试 | tools/approval/（纯逻辑层）；tests/approval/ 34 passed；未接真实执行路径 |
| run-manifest 派发前置清单（备忘九.2） | ✅ 已实现+单测级 | 桥 process() 派发前 write-once 持久化 + kickoff 引用 sha256 + resume 只读；模型/RAG/Skill 内容哈希诚实标 missing（待 worker 侧上报） |
| 成本计量脚手架（备忘四.4） | ✅ 脚手架+单测级 | tools/costmeter/ 预算守卫（预留/重试/结算/超限/并发/缺失/崩溃恢复 16 单测）+ 本地 span 计数收集器；**未接真实路径**（token 源在 OTel 外部 collector，预算金额未拍板） |
| 真实集成授权请求集中化 | ✅ 清单已出 | INTEGRATION-AUTH-REQUESTS.md（R1-R6：目标/操作/影响/证据/回退/权限），等待用户逐项授权 |

## 已验证结果（证据，2026-09-22 实测 @ 工作树）

- `python -X utf8 -m pytest tests/gh_bridge/ -q` → **36 passed**（23 M1 + 13 run-manifest）
- `python -X utf8 -m pytest tests/approval/ -q` → **34 passed**（M2-A）
- `python -X utf8 -m pytest tests/costmeter/ -q` → **16 passed**（成本脚手架逻辑层）
- `python -X utf8 -m pytest tests/gh_app/ -q` → **816 passed, 5 skipped（821 collected）**
  - **勘误**：本文件前版记录"831 passed"在当前干净树（8b30fb1，tests/gh_app 自 b46e8ba 字节未变）不可复现；821 为本轮两次独立实测一致值。差额 15 疑为当时混入未跟踪文件或转抄误差，不影响 M1 结论（无失败）。
- M1 九场景状态：1/2/3/4/5/7/8 ✅单测；9 ✅契约+单测；6 🔒结构保证（真实双 head 实证待授权）

## 当前阻塞（外部条件）

- **真实集成轮整体待授权**：R1-R6 见 INTEGRATION-AUTH-REQUESTS.md（M1 场景 3/6/7 的实证、双 head、运行副本同步、usage 源）。
- 密钥轮换：用户决定挂起（M4 出口条件）。
- D-1/D-2/D-3（审批动作集/审批人映射/TTL）产品拍板：阻塞门 Web 化与真实审批启用。
- 预算金额拍板：阻塞成本硬预算接入真实路径。

## 下一条可执行动作（按序）

1. **R5 只读探查**（零授权成本）：ctrl/worker 内模型标识、Skill 内容哈希、RAG 版本的可得位置——补 run-manifest missing 项的上报方案；
2. **门 Web 化预研**：M2 票据存储落点方案对比（PG approvals 改造 vs MinIO 票据对象），输出预研记录（依赖 D-1/D-2 拍板的仅是启用，不阻塞方案设计）；
3. **待授权项**：用户批复 R1-R4 后按建议顺序执行真实集成轮。

## 提交记录

- `b46e8ba` M1-1 发布语义加固 + 16 单测 + gh_app 路径修复 + 验收记录初始化
- `0baa644` M1-2 崩溃恢复 + M1-3 互斥契约 + 7 单测
- `8b30fb1` docs: STATUS 更新（M1 单测级收口）
- 本轮（M2-A）：M2-APPROVAL-SPEC.md + tools/approval/ + tests/approval/ + 契约 §6 + 记录勘误——**本地提交，未 push，待用户 review**
- 分支：`chore/backfill-r3-ops`（含此前的 r3ops 回流 f848196）——**未 push**

## 环境事实（2026-09-22 实测）

- 本地栈 8 容器 Up 30h+（ctrl/proxy/case-pg/element-web/4 workers）；服务器 ingress healthz ok；桥未运行（正常）。
- 运行中的桥副本在 `D:\goai\r3work\scripts\gh_bridge.py`——**repo 副本现为事实源，下次跑案例前需同步过去**（见 DECISIONS #3）。
- 互斥边界客观事实：桥 claim 不写 lease_expires_at ⇒ github_drain 过期接管谓词对桥在途行恒不成立（契约 §6）；cutover 三步程序已记入契约。
