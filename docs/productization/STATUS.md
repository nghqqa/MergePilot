# MergePilot 产品化推进状态（STATUS）

**更新**：2026-09-22 ｜ **分支**：`chore/backfill-r3-ops`（连续提交 M1 工作项）｜ 授权范围：M1→M4 至 V0 技术就绪

## 当前里程碑：M1 可靠性接管（进行中）

| 工作项 | 状态 | 说明 |
|---|---|---|
| M1-0 调用链与状态归属调研 | ✅ 完成 | 结论见 DECISIONS.md #1 |
| M1-0 验收矩阵 | ✅ 完成 | ACCEPTANCE.md |
| M1-1 发布语义加固（场景7/3/4/5/8 部分） | ✅ 已实现+已测试 | commit（本文件同批）；16 项单测全绿 + gh_app 831 回归全绿 |
| M1-2 崩溃恢复与续接（场景1/2） | 进行中 | stale RUNNING 租约接管 + 按项目权威状态续接 |
| M1-3 场景6/9 | 待做 | PR 更新失效验证 + 新旧编排互斥契约 |

## 已验证结果（证据）

- `python -X utf8 -m pytest tests/gh_bridge/ -q` → **16 passed**（发布语义：回写成功才 PROCESSED/对账采纳/有界重试/精确 claim 终结/去重守卫/timeout 不算完成）
- `python -X utf8 -m pytest tests/gh_app/ -q` → **831 passed, 5 skipped**（修复了仓库整理遗留的 8 个 Dockerfile 路径失败——测试引用根目录路径，文件已移入 docker/）

## 当前阻塞

无。（rag-live :4184 未运行——仅跑案例需要，M1 不依赖；密钥轮换按用户决定挂起）

## 下一条可执行动作

M1-2：在 gh_bridge 增加 `recover_stale()`——`run` 启动时接管过期 RUNNING（CAS 租约接管），按 MinIO 项目权威状态续接（receipt→直接终结；项目终态→续发布；非终态→续观察；无项目→有界回队 PENDING）。

## 环境事实（2026-09-22 实测）

- 本地栈 8 容器 Up 30h（ctrl/proxy/case-pg/element-web/4 workers）；服务器 ingress healthz ok；桥未运行（正常）。
- 运行中的桥副本在 `D:\goai\r3work\scripts\gh_bridge.py`——**repo 副本现为事实源，下次跑案例前需同步过去**（见 DECISIONS #3）。
