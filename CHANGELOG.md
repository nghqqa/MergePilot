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
