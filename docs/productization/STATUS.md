# MergePilot 产品化推进状态（STATUS）

**更新**：2026-09-22 ｜ **分支**：`chore/backfill-r3-ops`（连续提交 M1 工作项）｜ 授权范围：M1→M4 至 V0 技术就绪

## 当前里程碑：M1 可靠性接管（进行中）

| 工作项 | 状态 | 说明 |
|---|---|---|
| M1-0 调用链与状态归属调研 | ✅ 完成 | 结论见 DECISIONS.md #1 |
| M1-0 验收矩阵 | ✅ 完成 | ACCEPTANCE.md |
| M1-1 发布语义加固（场景7/3/4/5/8 部分） | ✅ 已实现+已测试 | commit（本文件同批）；16 项单测全绿 + gh_app 831 回归全绿 |
| M1-2 崩溃恢复与续接（场景1/2） | ✅ 已实现+已测试 | commit 0baa644；take_over_stale/resume/RQn 有界回队 |
| M1-3 场景9 互斥契约 | ✅ 契约+单测 | ORCHESTRATION-CONTRACT.md（claim 命名空间/精确 CAS/接管边界） |
| M1-3 场景6 PR 更新失效 | 🔒 结构保证+单测 | check-run 绑 head_sha + already_processed 按 head 去重；真实双 head 实证待授权案例轮 |

## 已验证结果（证据）

- `python -X utf8 -m pytest tests/gh_bridge/ -q` → **23 passed**（M1-1 发布语义 16 + M1-2 恢复语义 7：场景 1/2/3/4/5/7/8 单测级全覆盖）
- `python -X utf8 -m pytest tests/gh_app/ -q` → **831 passed, 5 skipped**（修复了仓库整理遗留的 8 个 Dockerfile 路径失败）
- M1 九场景状态：1/2/3/4/5/7/8 ✅单测；9 ✅契约+单测；6 🔒结构保证（真实双 head 实证待授权）

## 当前阻塞

无。（rag-live :4184 未运行——仅跑案例需要，M1 不依赖；密钥轮换按用户决定挂起）

## 下一条可执行动作（按序）

1. **M2 本地可做部分**：审批票据语义规格（L2 四问的动作/绑定/失效/竞争四要素）+ 绑定校验器与单测——为门 Web 化打地基；
2. **run 级版本清单（备忘九.2）**：桥在 kickoff 后写 MinIO run-manifest（镜像标识 + 激活配置哈希 + 桥版本），恢复时校验——半天量级；
3. **成本计量脚手架**：usage 汇总脚本（数据源=agentloop spans/skill 审计）+ 单 run 硬预算断言挂点；
4. **待授权项集中提出**（真实集成轮）：服务器 PG 下故障注入（kill -9 各阶段）、真实 GitHub reconcile/POST、场景6 双 head 实证、运行副本同步 r3work 后跑一轮真实案例回归。

## 提交记录

- `b46e8ba` M1-1 发布语义加固 + 16 单测 + gh_app 路径修复 + 验收记录初始化
- `0baa644` M1-2 崩溃恢复 + M1-3 互斥契约 + 7 单测
- 分支：`chore/backfill-r3-ops`（含此前的 r3ops 回流 f848196）——**未 push，待用户 review**

## 环境事实（2026-09-22 实测）

- 本地栈 8 容器 Up 30h（ctrl/proxy/case-pg/element-web/4 workers）；服务器 ingress healthz ok；桥未运行（正常）。
- 运行中的桥副本在 `D:\goai\r3work\scripts\gh_bridge.py`——**repo 副本现为事实源，下次跑案例前需同步过去**（见 DECISIONS #3）。
