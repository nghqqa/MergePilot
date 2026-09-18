# 提交材料 · Submission materials

GOAI 2026 决赛提交包中与代码仓库对应的文档与演示材料（完整提交包含方案 PDF、演示视频、正式路演 PPT，体积原因不入库）。

| 文件 | 说明 |
| --- | --- |
| `SKILLS.md` | 六类 Skill 的契约、执行方式与可观测性边界（分级如实口径：已云端确认样本 / 本地契约审计） |
| `mergepilot-schema-review/` | 跨仓 Schema 变更审查 Skill（含 YAML frontmatter 的 SKILL.md 与安装说明） |
| `DEPLOY.md` | 演示平台部署说明 |
| `.env.example` | 演示平台环境变量示例（无真实凭据） |
| `roadshow-swiss/` | 路演备用网页版：`index.html` 浏览器打开，←→ 翻页，`P` 演讲者模式，`B` 静态模式；同版 PDF 随附 |

可运行演示平台在仓库根目录 `demo-platform/`（`node backend/server.mjs`，自测 `node backend/test/selftest.mjs`）。

## 数据边界（冻结口径）

RAG：SYNTHETIC / REDACTED · Database Branch：SIMULATED · PolarDB：NOT CONNECTED · PR Auto Merge：DISABLED · AgentLoop：云端 Trace 为已确认样本 n=1，非当前实时数据。
