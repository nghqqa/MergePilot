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

## 已验证结果（证据，2026-09-22 实测 @ 工作树）

- `python -X utf8 -m pytest tests/gh_bridge/ -q` → **36 passed**（23 M1 + 13 run-manifest）
- `python -X utf8 -m pytest tests/approval/ -q` → **34 passed**（M2-A）
- `python -X utf8 -m pytest tests/gh_app/ -q` → **816 passed, 5 skipped（821 collected）**
  - **勘误**：本文件前版记录"831 passed"在当前干净树（8b30fb1，tests/gh_app 自 b46e8ba 字节未变）不可复现；821 为本轮两次独立实测一致值。差额 15 疑为当时混入未跟踪文件或转抄误差，不影响 M1 结论（无失败）。
- M1 九场景状态：1/2/3/4/5/7/8 ✅单测；9 ✅契约+单测；6 🔒结构保证（真实双 head 实证待授权）

## 当前阻塞

无本地阻塞。（rag-live :4184 未运行——仅跑案例需要，M1/M2 不依赖；密钥轮换按用户决定挂起，M4 出口条件见 DECISIONS #5）

## 下一条可执行动作（按序）

1. **成本计量脚手架**：usage 汇总脚本（数据源=agentloop spans/skill 审计）+ 单 run 硬预算断言挂点；
2. **worker 侧版本上报**：run-manifest 的 missing 项（模型标识/Skill 内容哈希/RAG 版本）从 worker/ctrl 侧取得并补录——需读 ctrl 容器 agt/agentloop 接口；
3. **门 Web 化预研**：M2 票据存储落点（PG approvals 改造 vs MinIO 票据对象），依赖 D-1/D-2 拍板；
4. **待授权项集中提出**（真实集成轮）：服务器 PG 下故障注入（kill -9 各阶段）、真实 GitHub reconcile/POST、场景6 双 head 实证、运行副本同步 r3work 后真实案例回归（含真实 run-manifest 落盘验证）。

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
