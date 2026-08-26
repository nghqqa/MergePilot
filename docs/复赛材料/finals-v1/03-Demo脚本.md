# MergePilot 复赛 Demo 脚本（首次提交候选）

成片时长约 4 分 53 秒。固定产品身份：`v0.1.0-preview.4 @ 5bb2635`。旁白采用 AI 合成语音，技术内容与验收边界由团队人工复核。

1. **产品定位（0:00–0:08）**：说明 MergePilot 是面向 GitHub PR 场景的多 Agent 审核与风险治理系统。
2. **入口与运行边界（0:08–0:35）**：说明当前为 Preview 4 预发布版本，并展示 Release、commit、HTTP 200、Windows 11 + WSL2、loopback-only、`wsl-user-relay` 和 `direct_routing_verified=false`。
3. **控制台总览（0:35–1:11）**：展示 17 阶段、只读投影、`no-store` 策略和五项 `NOT_VERIFIED` 真实性边界；明确画面为冻结同机验收证据，不代表客户或生产数据。
4. **Complete 证据（1:11–1:46）**：展示 17 stages、16/16 prerequisites、6/6 route edges、Receipt 和 Matrix；明确中继路径不冒充直接路由。
5. **Failed / stale（1:46–2:22）**：展示首个稳定错误 `E2E_ROUTE_PROBE_FAILED`、失败阶段、pending 后续阶段和 stale 拒绝；不把演示投影说成真实生产事故。
6. **治理架构（2:22–3:02）**：说明 4 个运行时 Agent、6 类 Skill、Workflow Controller、Policy Gateway 和 GitHub MCP 隔离服务；Worker 不持有 PAT。
7. **离线生命周期（3:02–3:46）**：展示 9 个 OCI 镜像、约 847MB、校验和与镜像集合检查、七动作合同，以及 HTTP 200/405/404/403 边界。
8. **清理与所有权边界（3:46–4:27）**：展示首次 Cleanup 消费 `install.json` 并只删除属主镜像；第二次 Cleanup 在 manifest 从一开始不存在时 fail-closed；journal 保留、secrets 清空、运行残留归零。
9. **结论与下一步（4:27–4:53）**：冻结 `SAME_MACHINE_ACCEPTED`、`EXTERNAL_BLOCKED`、`2471 passed / 0 failed / 20 skipped`；说明跨平台 Provider 化、Linux、macOS 和云端验证仍属于后续工作。

## 展示纪律

- 画面中的版本、commit、测试计数和真实性边界必须与正式 Release 一致。
- Complete、failed、stale 均说明数据来源；失败投影不冒充生产事故。
- 不声称 production ready，不将同机验收扩展为独立物理机或生产环境验收。
- 现场环境不可控时直接播放冻结视频，不临时修改或伪造运行结果。

## 备份材料

- 正式 Release 与 README；
- `Check`、`Status`、lifecycle、Cleanup 的冻结证据；
- Complete / failed / stale 控制台截图；
- 更新版 PPT/PDF 与声明证据矩阵。
