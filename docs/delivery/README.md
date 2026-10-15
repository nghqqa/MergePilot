# docs/delivery/ — 交付文档（rc.35.4 口径）

面向**新用户/部署者**的四件套 + 能力事实。当前产品已是完整审查链
（webhook→静态规则→模型审查→发现→审批→补丁验证→人工发布评论）；
「应用修复/自动重审/CI 消费/合并」为开发中能力，文档中一律显式标注。

| 文档 | 读它解决什么 |
|---|---|
| [CAPABILITY-MATRIX.md](CAPABILITY-MATRIX.md) | 每个能力的状态与真实依赖（env/DB/执行器/凭据/模型授权）——事实锚点 |
| [DEPLOY-GUIDE.md](DEPLOY-GUIDE.md) | 按目标分步部署：最小部署→LLM 审查→修复验证→（预告）提交与合并 |
| [USER-GUIDE.md](USER-GUIDE.md) | 日常使用：登录/接入/看发现/读懂验证三分/审批/发布评论 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 常见失败的具体处理步骤（诊断工具失败项指向这里） |
| [ACCEPTANCE-CHECKLIST.md](ACCEPTANCE-CHECKLIST.md) | 新用户逐项验收清单（可打勾） |

配套只读诊断工具：[`scripts/delivery/diagnose.mjs`](../../scripts/delivery/diagnose.mjs)
（人可读 + `--json` 双输出；退出码 0=全绿/1=有失败项/2=用法错误；凭据零输出、不自动改配置）。

事实源：能力矩阵各条目均标注代码出处（§4）；文档与代码不一致时，以代码为准并请反馈修正文档。
