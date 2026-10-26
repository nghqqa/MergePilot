# FROZEN — 项目冻结与事件响应维护状态

> 状态：**事件响应维护（frozen / event-driven maintenance）**，自 2026-10-26 起。
> 本文件是冻结状态的唯一权威说明；与本文冲突的旧文档段落以本文为准。

## 1. 当前事实基线（2026-10-26 核对）

| 项 | 值 | 状态 |
|---|---|---|
| main 分支 | `ff71273`（以 main 最新为准） | 已发布基线的祖先链 |
| rc8.4.1 tag | `v0.2.0-beta.6-rc.35.6-rc8.4.1` → commit `ba089624e266f7aa14dda3f45285a3232b7f5938` | **已发布**（GHCR + GitHub Release + 离线包） |
| 生产 console | 镜像 `sha256:c874b4d88ef6…5cbd3`（= rc8.4.1 index），容器 `beta-mp-console` | **已部署**；容器当前处于**计划内停止**状态（数据在卷，`deploy/mergepilot-stack/start-prod.ps1` 一键恢复），restarts=0 |
| 安全姿态 | trivy 漏洞全零 / secrets=0；基础镜像 CVE-2026-111497（brotli）已双面闭合（node 22.23.4 + brotli-libs 1.2.1-r0） | 已验收 |
| 登录与权限 | A/B/C/D 四角色真实 OAuth（GitHub platform_admin/contributor、Gitee maintainer/auditor）+ 服务端授权边界逐项实测，全部与 `authz.mjs` 一致 | 已验收 |
| 观察窗 | rc8.4.1 换版后 T+1h：22/22 有效样本全绿、restarts=0、错误日志 0 行 | 已通过 |
| schema | v29（rc8.3→rc8.4.1 零迁移） | 稳定 |

**如实边界（未验证/未覆盖，勿当已完成）**：真实业务流量与非空审查管线仅历史样本；
真实第三方用户自托管首装未发生；移动端、200% 缩放、读屏器未验证；
auditor 角色的验收账号已于验收后**按授权撤销**（后续状态未定）；
Gitee OAuth 复验仅覆盖 maintainer 一路径；审批/修复/合并等受控写不在本轮复验范围。

## 2. 三种状态口径（禁止混用）

- **已发布（released）**：制品在 GHCR/GitHub Release 不可变存在，可下载可核验。
- **已部署（deployed）**：某环境实际运行该版本（以该环境 `/api/health` version+commit 为唯一证据）。
- **待处理（pending）**：已登记、未执行、未验证的事项——**任何文档不得把待处理写成完成**。

## 3. 冻结规则

1. `main` **只接受安全补丁类 PR**（上游 CVE 闭合、依赖安全修复）；功能 PR 一律不受理。
2. 现有 CI 门（console backend + frontend gate、selftest、secret-scan 等）**不变、不减、不跳过**。
3. 任何补丁版发布沿用既有流程：干净检出 → buildx → GHCR → Release（附离线包/SBOM/扫描/SHA256SUMS）→ 生产换版**另行单独授权**。
4. 不新增月度/定时自动化任务；巡检为手动事件（见 §5）。

## 4. 事件触发条件与响应路径

| 触发源 | 事件 | 响应路径 |
|---|---|---|
| 上游安全 | node:22-alpine / alpine 仓库链出现 **HIGH/CRITICAL** CVE（如 brotli、zlib 先例） | 补丁版：升基础 digest（禁浮动 tag）→ 必要时 apk 显式升级 → 干净重建 → Release；生产换版另批 |
| 平台漂移 | GitHub App / OAuth / Gitee API 行为变更导致冻结代码失效（用户报障触发） | 兼容性修复：单点修复 PR（最小 diff），走完整 CI |
| 真实用户 | 外部自托管安装失败或真实 bug 报告 | 先定性（文档问题→改文档；产品缺陷→按严重度立项）；**安装文档 + INSTALL-WITH-AI.md 同步修订** |
| 明确业务需求 | 真实运营方提出功能需求 | 需求立项：解冻评估（范围/代价/验收标准）单独决策，默认不做 |

## 5. 每月只读巡检（手动执行，不设自动化）

**范围**（全部只读）：
1. Node.js 安全通告与 alpine 安全仓库（brotli/zlib/openssl 链）——是否出现 HIGH/CRITICAL；
2. GitHub（App/OAuth/webhook API）与 Gitee 开放平台 changelog——是否有破坏性变更公告；
3. `ghcr.io/nghqqa/mergepilot-console` 镜像可拉取性与 tag 完整性（匿名核验）；
4. docs.nghqqa.cn 站点可达性与当前页版本一致性；
5. 本仓库 main 与最新 tag 的关系（确认无人误推功能提交）。

**输出格式**（巡检结论一行制）：
`YYYY-MM-DD 巡检：项1=无需动作/需动作(<说明>)；项2=…；结论=维持冻结/触发<响应路径>`。
巡检不写代码、不重建镜像、不改配置；需要动作时按 §4 路径另行立项。

## 6. 当前未决台账（pending——全部未执行，勿当已完成）

| # | 事项 | 状态 |
|---|---|---|
| 1 | B 组具名卷 25 个（含模型卷 3×~2.13GB：iso4v-bge-models / mergepilot-v4_model-cache / mp-rag-models；试用/iso/验收数据卷；qwp 评测卷） | 待逐群决定去留 |
| 2 | C 类 env 文件：`r3work\accept-deploy-468\deploy\selfhost\.env/.env.fixpack/.env.trial`（被试用栈工具链引用+LLM key 可能仍有效）、`mp-c-verify\.env` | 待定（引用未解除前不可移/删） |
| 3 | `archive\…\mp-gh4-secrets\mcp-bridge-fgpat.txt`（fine-grained PAT，无撤销记录） | 待用户确认 GitHub 侧状态后处置 |
| 4 | `r3work\rc841-release`（官方发布构建用 git worktree，干净） | 待 `git worktree remove` 正规移除 |
| 5 | MIMOSA 工具快照（.mimosa hook-state，数千文件含历史文件副本） | 待整体归档/删除决策 |
| 6 | `r3work-archive\`（7.82GB，223 项已归档波次） | 待外置/加密/保留策略 |

> 台账纪律：以上任何一项执行时**重新做该项目的删前/移前核对**（引用、哈希、保护集），
> 不依赖本表过期信息。

## 7. 冻结期操作红线

- 不改运行时代码、CI、发布制品（除非 §4 触发且走 §3 流程）；
- 不动生产容器/卷/数据与 `prod-data\`、`secrets\`、`frp\frpc.toml`（现役保护集）；
- 不删除回滚链镜像（rc7 / rc8 / rc8.1~8.4 / candidate.1 / candidate.2）与 PG 备份；
- 删除/移动任何资产前，先做该资产的引用面与哈希核对（今日先例：孤儿卷盲区修复、
  截断哈希名三次教训——**哈希探针与保护集只准使用完整名称**）。
