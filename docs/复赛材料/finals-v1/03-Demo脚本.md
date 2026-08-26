# MergePilot 复赛 Demo 脚本（现场版 / 录屏备份版）

目标 6–7 分钟。现场与录屏使用相同顺序，录屏额外保存命令输出和时间戳。固定版本：`v0.1.0-preview.4 @ 5bb2635`。

1. **入口（0:00–0:40）**：展示 Release，运行 `Check`/`Status`。说明 Windows 11 + WSL2、loopback-only、`wsl-user-relay`、`direct_routing_verified=false`。
2. **控制台（0:40–1:30）**：打开 `http://127.0.0.1:8600/e2e-status.html`，展示状态带、17 阶段、只读页面和五项 NOT_VERIFIED。
3. **complete（1:30–2:30）**：展示 17 stages、16/16 prerequisites、6/6 route edges、Receipt。明确这是同机 Preview 证据。
4. **failed/stale（2:30–3:25）**：展示首个稳定错误、失败边、pending 后续阶段和 stale 拒绝；不把失败投影说成生产事故。
5. **治理边界（3:25–4:20）**：说明 4 Agent/6 职责、6 Skill DAG、Controller、Gateway 和 GitHub MCP 隔离服务；Worker 不持 PAT。
6. **生命周期（4:20–5:25）**：展示 9 镜像、约 847MB、checksum 先验、Install/Doctor/Start/Status、HTTP 200/405/404/403。
7. **清理与所有权边界（5:25–6:20）**：展示 Stop→再 Start→受控失败→Cleanup；第一次 Cleanup 报 manifest consumed，第二次报从开始不存在；展示零残留和非属主镜像保留。
8. **收束（6:20–6:50）**：说明 `SAME_MACHINE_ACCEPTED`、`EXTERNAL_BLOCKED`、`2471/20`，以及下一步独立机器验收与更大样本。

备份：预录控制台截图、Check/Status 输出、lifecycle 和 cleanup evidence。现场不执行真实 GitHub 写入，不展示秘密，不伪造实时或生产数据。
