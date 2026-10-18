# docs/delivery/ — 交付文档（rc.35.5 口径）

面向**新用户/部署者**的五件套 + 能力事实。当前产品是**完整受控闭环**：
webhook→静态/模型审查→发现→审批→补丁与验证证据→**预览并确认应用（机器人提交）→
新 head 自动重审→该 head 真实 CI→预览并确认合并**，全程审计留痕。
每一步远端写（评论/应用/合并）都需维护者逐次「预览 → 确认」；系统不自动改代码、
不自动合并、不绕过平台保护。

| 文档 | 读它解决什么 |
|---|---|
| [USER-GUIDE.md](USER-GUIDE.md) | **首次使用入口**：受邀用户四步上手 / 自托管管理员按能力启用；PR 事件→…→合并与审计的 10 步完整路径 |
| [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md) | 每个能力的状态与真实依赖（env/DB/执行器/凭据/模型授权）——事实锚点 |
| [DEPLOY-GUIDE.md](DEPLOY-GUIDE.md) | 按目标分步部署：最小部署→LLM 审查→修复验证→应用与合并 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 常见失败的具体处理步骤（诊断工具失败项指向这里） |
| [ACCEPTANCE-CHECKLIST.md](ACCEPTANCE-CHECKLIST.md) | 新用户逐项验收清单（可打勾） |
| [DEMO-PR12.md](DEMO-PR12.md) | 15 分钟端到端演示脚本（复用真实仓库 PR#12 已有证据，演示过程零新写入） |
| [RELEASE-rc.35.5.md](RELEASE-rc.35.5.md) | 本版本发布材料与回滚方案（冻结身份/镜像 digest/能力边界） |
| [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) | 每次发布必须逐项执行的检查清单（构建身份/health 核对/扫描/匿名校验/T+1h） |

配套只读诊断工具：[`scripts/delivery/diagnose.mjs`](../../scripts/delivery/diagnose.mjs)
（人可读 + `--json` 双输出；退出码 0=全绿/1=有失败项/2=用法错误；凭据零输出、不自动改配置）。

事实源：能力矩阵各条目均标注代码出处（§4）；文档与代码不一致时，以代码为准并请反馈修正文档。
