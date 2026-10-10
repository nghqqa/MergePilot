# Forge 平台支持矩阵（Gitee 首版）

- 日期：2026-10-10（Gitee G-1~G-5 交付）
- 口径：**能力 × 平台 × 状态**——「已实现」必须有代码+测试证据；「未提供」=本接入未实现（非平台能力结论）；「试点后启用」=代码就绪、需真实凭据/试点验收。

## 状态图例

| 标记 | 含义 |
|---|---|
| ✅ 已实现（隔离验证） | 代码+单测/集成测试通过（真实服务代码+stub 外部 API）；生产启用待发布序列 |
| 🔬 试点后启用 | 实现就绪，需真实 Gitee 试点仓库+令牌验证后才能对生产启用 |
| ⛔ 本接入未提供 | 首版范围决策；**不构成平台能力结论**（404 探针只证明所测请求失败） |
| — | GitHub 现有链（本轮零改动） |

## 平台 × 能力矩阵

| 能力 | GitHub（legacy） | Gitee（首版） |
|---|---|---|
| 仓库连接模型 | installation（App 安装），本轮不动 | forge_connection（v24）✅ 隔离验证；credential_ref=部署 env（**单一部署凭据**，非每租户独立——限制如实呈现） |
| 连接管理 API | — | 登记/列表/probe/revoke/绑定仓库 ✅（manage_instance 门；跨租户隔离 ✅） |
| webhook 入口 | /api/mu/github/webhook（HMAC body 签名）— | /api/mu/gitee/webhook ✅ 显式单模式验真（signature/password 由部署 env 选择）；**未配置→503 不启用**；签名实际携带方式 🔬 试点确认后收敛 |
| 手动审查入口 | #379 真实链 — | 同一消费单元 ✅（审计保留真实触发用户） |
| 事件契约 | legacy 7 字段（#391 锁 51/51 回归 ✅） | 规范事件 v1（入队前+消费前双校验）✅ |
| PR 上下文读取 | ghprovider（v3.diff 全文）— | GiteeAdapter：分页 files→per-file patch（patch.diff 对象解包+类型校验）✅ |
| completeness | — | complete/partial/unknown 三态 ✅；**unknown→dry-run 拒绝**（D5 门控 ✅） |
| 检查读取 | check_runs — | ⛔ 本接入未提供（零调用；UI 呈现"本接入未提供"） |
| 保护读取 | protection API — | ⛔ 本接入未提供（合并资格按 unknown fail-closed，UI 明示"非未受保护"） |
| 审查→审批→dry-run | 全链 ✅（既有） | 复用同一状态机 ✅（隔离集成 28 项：clean 完成/审批门 WAITING/零远端写） |
| 写回（评论/状态/补丁/merge） | 零写入 — | ⛔ 首版范围外（调用面 GET 白名单断言 ✅） |
| Gitee 登录 | — | ⛔ 本轮不做（G-6 独立；设置页明示仓库接入≠登录） |

## 边界声明

1. Gitee 支持限定**云端（gitee.com）公共 OpenAPI v5**；企业版/私有化不在本版承诺内。
2. 单一部署凭据（MU_GITEE_PAT env）为已知限制——每租户独立凭据属后续演进，不伪装已隔离。
3. 真实 Gitee 生产接入的启用条件：试点仓库+令牌就绪、真实 webhook 全动作采集回填、probeConnection 验证通过（见 GITEE-G1G5-ACCEPTANCE.md §试点前置）。
