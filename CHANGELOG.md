## [0.2.0-beta.6-rc.22] — 2026-10-09（候选）

### Fixed
- **手动审查 event_sync payload 修复**：payload 缺少 pr_number/github_repo_id 导致消费时 event_payload_invalid → failed（生产实证）——补齐字段后手动「触发只读审查」可跑通完整真实管线（#389）
- payload 契约回归锁：字段齐全断言 + 缺字段明确失败 event_payload_invalid（#391）

### Changed（文档）
- deploy/selfhost/README.md 补「角色与权限速查」5×9 矩阵表（#388）
- distribution/docs/SECURITY.md 补角色与权限一节（#388）
- CorePage 403 文案加文档指引（#388）

**零 schema/迁移变更（恒 v23，与 rc.21 一致）。**

## [0.2.0-beta.6-rc.21] — 2026-10-09（候选）

### Added
- **共享错误文案助手**（api-error-copy.js）：按机器码/HTTP 状态区分会话/授权/依赖缺失/状态冲突/系统故障，直白文案不含实现术语；CorePage/SkillsPage 对齐
- **fixture 遗留任务取消端点**：POST /api/mu/jobs/:id/cancel（平台管理员）——仅 queued 的人工 job 可取消=rejected+审计；系统事件任务不可取消

### Fixed
- SkillsPage 原因词表增补 action_not_granted/binding_required 直白文案

**零 schema/迁移变更（恒 v23，与 rc.20 一致）。**

## [0.2.0-beta.6-rc.20] — 2026-10-09（候选）

### Fixed（核心链阻断与误导语义）
- **手动「触发只读审查」接入真实消费链**：此前入队 fixture 路径任务（review_run），生产（MU_FIXTURES 未设）永不消费、永久排队误导用户——现与 webhook 同一消费单元（event_sync 系统级入队→真实确定性审查），fixture 路径仅保留于显式测试模式
- **手动「发起受控修复」遵循审批门语义**：等待审批时返回待批清单引导逐条批准（零入队）；不再产生永不消费的 repair_push
- **FIX_QUEUED 恢复入口**：修复轮被跳过/门控失败的 run 可经修复端点合法重试（authorizeFixExecution 对 CONSUMED 幂等——审批不二次消费；执行器门 fail-closed 如实返回，run 保持可恢复态）
- **修复轮静默跳过补审计**：at_ensure_failed / at_workers_detail_query_failed / at_submit_failed 三分支 + decide 流 FIX_ROUND_SKIPPED 审计——每个跳过均有可定位原因
- **CONSUMED 票面语义修正**：「已消费（修复预演已启动）」过度承诺 → 「已批准并放行修复轮——是否实际执行以审查管线面板为准」

### Changed（权限与文案）
- **403 授权态与故障态语义分离**：系统状态页 action_not_granted 不再渲染为"请求失败"；共享错误文案助手（api-error-copy）区分会话/授权/依赖缺失/状态冲突/系统故障，直白文案不使用实现术语
- SkillsPage 原因词表增补 action_not_granted/binding_required 直白文案

### Added（运维）
- **fixture 遗留任务取消端点**：POST /api/mu/jobs/:id/cancel（平台管理员）——仅 queued 的人工 job 可取消=rejected+审计；系统事件任务不可取消
- recover-agentteams 完成判定对齐执行器门（浅层注册不得报成功，缺绑定引导完整置备）#377

**零 schema/迁移变更（恒 v23，与 rc.19 一致）。**

## [0.2.0-beta.6-rc.19] — 2026-10-08（候选）

### Changed / Fixed（品牌与可读性）
- **品牌区去 V0 标签**：左上角品牌区呈现 MergePilot / 审查工作台（与登录页口径一致）；删除装饰性阶段徽章及其同步引入的废弃样式，无替代徽章、不硬编码版本
- **移动端导航抽屉品牌可读性**：antd Drawer 头部默认白底导致 "MergePilot"（#fff）白字白底不可见——头部/关闭钮改用侧栏同源主题令牌（--c-nav/--c-nav-text/--c-nav-line），body 背景去硬编码；对比度 #fff/#0b0f19≈17.9:1、副行 ≈4.7:1（AA 口径）

### Added（运维交付，非运行时行为）
- **AgentTeams 认证代理运维包**（deploy/agentteams-beta/auth-proxy/）：nginx 配置模板+部署器+ctrl 重建后注册表恢复工具（幂等，走 ensureFourAgents 生产契约）与运维说明；RUNBOOK 补「重建≠重启」语义（重建即撤销全部历史 Bearer token）与 agt rotate 未实测候选声明

### Fixed（运维工具）
- swap-console 输出安全化：dry-run/执行/失败路径不再回显完整 docker run 参数与 env 值（对抗性 stderr 夹具覆盖）

**零 schema/迁移变更（恒 v23，与 rc.18 一致）。**

## [0.2.0-beta.6-rc.18] — 2026-10-07（候选）

### Added / Improved（前端阅读体验）
- **PR 详情信息精简**：三层结构（摘要→发现/审批→技术详情折叠）；保护未知/合并资格未知/fail-closed 三处合并为单一入口并修正操作路径卡片历史未渲染的传参错位；移动端发现/审批纵向列表（≤760px 无横向滚动）
- **字号层级**：内容性文字 14px 正文色（发现摘要/修复建议/阻塞原因/权限提示/审批说明），简短辅助 13px，真元数据 12px；`--fs-xs` 11.5→12px，全站 11px 清零；正常文字对比度 ≥5:1（WCAG AA ≥4.5:1）
- 阻塞原因/权限不足直接可见（不再仅 hover tooltip）；审批旁保留「批准仅启动 dry-run，不写 GitHub、不自动合并」一句说明

### Fixed（自托管）
- preflight 镜像版本探测对齐 compose 实际引用（宽松匹配误报修复）并导出 parseImageRef/pickLocalImage 可测纯函数
- 首个平台管理员初始化文档：新增执行方式与授权生命周期说明（ON_ERROR_STOP 口径/退出码语义/重跑幂等边界/收权范围）

## [0.2.0-beta.6-rc.17] — 2026-10-07

### Added / Fixed
- start?invite= 非法格式统一 404；演示放行 modeDeclared/modeKnownNonMu 补全
- GHCR 包转 public（匿名可拉）；官网/文档全站 rc.17 对齐；OCI index/amd64 manifest/config digest 三标识分列
- 自托管文档：部署后首次使用章节、常见错误速查表、公网入口与 callback 边界声明

## [0.2.0-beta.6-rc.16] — 2026-10-06

### Security / Fixed
- **keystore 状态/验签统一以实际可加载密钥为准**：不可读/损坏密钥不再误报 READY；状态面转 BLOCKED（RUN_BINDING_KEYS_UNUSABLE，稳定原因+计数，零细节泄露）
- verify 端点对不可读/无可用密钥 fail-closed 返回 4xx（原 500）；BAD_SIGNATURE/REPLAYED_NONCE/TIMESTAMP_SKEW 语义不变
- README：cchain 三步引导（模型+manifest / fxv.audit_events 建表 / keystore 种子密钥+属主 uid 1000）

# Changelog

本文件记录 MergePilot 用户可感知的变更。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循语义化前缀的 beta 通道（`0.2.0-beta.6-rc.N`）。

## [0.2.0-beta.6-rc.15] — 2026-10-06

### Fixed
- **概览权威 head 元数据（PR #325）**：`mu-console-api` overview 响应携带权威 current head、
  分支保护按当前头判定、缺失/失败/零值三类分离——消除首屏"数据可信度"缺口（陈旧 head 误显示）。

### Added
- 批 1 发布资产：`CHANGELOG.md`、`deploy/selfhost/` 自托管套件
  （compose 模板 / .env.example / preflight 一键检查 / webhook-only ingress / 用户面 README）、
  版本号一致性（构建期 ARG 与运行时 fallback 同步）。

### Notes
- 本版本由多租户四项 READY 的 rc.14 谱系 + #325 修复组成；
  生产换容器部署与 GHCR 不可变 digest 发布另行批准（rc.15 晋级流程中）。

## [0.2.0-beta.6-rc.14] — 2026-10-06

### Added
- **邀请认领泛化（多租户 onboarding）**：既有用户可经待认领邀请进入另一租户——OAuth 登录时按
  GitHub subject 查找全部可认领邀请：唯一则自动认领并将会话绑定该租户；多条时返回
  `invitation_ambiguous` 显式拒绝（禁止静默回退）；零条保持既有行为。
- **多租户生产实证**：webhook→consumer 正向链路、独立第二外部用户接入、
  撤权后同会话下一请求即 403，全链在受控生产环境验收通过。

### Security
- D-3 守卫泛化：`platform_admin` 邀请拒绝逻辑在**新用户与既有用户两条 claim 路径共享**
  （先拒后认领，拒绝时不再消耗邀请行）；API 创建 403 与 DB v23 CHECK 维持不变。
- 会话 Cookie 保持 `HttpOnly + Secure + SameSite=Lax`；CSRF 双提交对管理面全覆盖。

### 升级路径
- 换镜像即完成升级；schema v23 已随 rc.13 落库，本版本**零 migration**。
- 回滚：换回旧镜像即可；schema 只前进不降级（见自托管 README「升级与回滚」）。

### 已知限制
- 暂无租户切换端点/UI（多租户用户需以对应租户的邀请重新登录，MT-ONB-3 规划中）。
- 私有 GitHub App 仅所有者账号可安装——多租户 webhook 接入需将 App 公开化或在
  各租户所有者账号上分别创建 App。

---

## [0.2.0-beta.6-rc.13] — 2026-10-05

### Security
- **v23 `invitation_role_check` 收紧**：`platform_admin` 从邀请可授予角色枚举中移除——
  外部成员准入路径（invitation）三层 fail-closed：API 403 `platform_admin_invitation_forbidden`、
  OAuth claim 服务端拒绝、DB CHECK 兜底。
- D-2：cchain 模型缓存完整性改为**流式哈希**（修复 >2GiB 模型文件读取崩溃）。
- D-1：keystore 轮换增加 MU 会话操作面（`manage_instance` + CSRF 双提交）。

### Added
- 租户边界（v22 PR-E）：八张业务表 tenant_id 强制列 + 复合 FK 边界 + 逐请求活成员校验。

---

## [0.2.0-beta.6-rc.12] — 2026-10-05

### Fixed
- cchain 模型缓存状态 500（readFileSync 2GiB 上限）——由 rc.13 流式哈希根治前的临时回滚锚。
- 事件自动 consumer（平台级，45s 节拍，advisory-lock 单赢家）；投递幂等（duplicate 语义）。

---

## [0.2.0-beta.6-rc.11] — 2026-10-05

### Added
- 版本号构建期注入（`MERGEPILOT_VERSION` Dockerfile ARG 为唯一真源）。
- 镜像内置版本号；consumer 公网投递自动消费与幂等。

---

## [0.2.0-beta.6-rc.9] — 2026-10-03

### Added
- bge-m3 模型安装与激活状态机（官方清单 SHA256 钉死、逐文件校验、R@5=0.9615 准入）。
- RAG 检索留痕（`rag_retrieval_event`）与技能调用留痕（`skill_invocation_event`）。

---

## [0.2.0-beta.6-rc.5 ~ rc.8] — 2026-10-02 ~ 10-03

### Security
- 审批硬门（v16）：Fixer 执行仅经 `WAITING` 审批票 + maintainer 决定（D-0）。
- 技能治理页（v17）：技能注册/发布/激活/回滚 E2E。
- 前端整改九件套与多用户边界（v18/v19）。

> 更早版本（beta.1~beta.6 通道）见仓库 release 页与 docs/ 设计档案。
