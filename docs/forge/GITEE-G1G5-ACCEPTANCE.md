# Gitee 接入 G-1~G-5 验收记录

- 日期：2026-10-10
- 分支：`gitee/forge-foundation` → `gitee/forge-consumer` → `gitee/forge-frontend`（栈式 PR，base=main=e7d5659）
- 层级纪律：测试分三层标注——**真实服务代码**（隔离 PG+真实 HTTP server+真实消费者状态机）、**stub 外部 API**（globalThis.fetch 拦截 gitee.com，同进程消费者经默认 fetch 装配适配器）、**真实平台**（本轮无——见 §试点前置，不冒充）。

## 一、测试证据

| 套件 | 覆盖 | 结果 |
|---|---|---|
| `console/backend/test/mu-forge.test.mjs` | 事件 v1 校验器（缺字段逐项可定位/installation 伪装拒绝/kind 白名单）、delivery_ref 确定性（无时间戳）、验真 signature/password 双模式+错签/超窗/缺字段、适配器八方法（patch.diff 对象解包/字符串数字转换/分页/completeness 三态/head 漂移/错误分类 401-429-5xx/超时）、**错误消息不含 token/URL**、白名单与凭据缺失 fail-closed、readChecks/readProtection 恒 not_provided 零调用 | 25/25 ✅ |
| `console/backend/test/mu-gitee-consumer.integration.mjs` | 隔离 PG+真实服务器：迁移 v24 幂等、连接 RBAC（maintainer 403/platform_admin ok）、probe 实测 valid、绑定、**手动审查→真实消费者→run/attempt→clean 完成（无发现也成功，不强造 finding）**、调用面全 GET 零写调用、同 head 幂等、重复投递 duplicate 零新 job、head 漂移→BLOCKED+gitee_head_moved、缺字段→event_payload_invalid 可定位、跨租户 job FK 拒绝、连接撤销→webhook ignored+存量 job rejected（重登记恢复验证）、P0→审批门 WAITING（不批不修）、错签名 401 零入队、token 不落路径、**GitHub legacy webhook+消费回归** | 28/28 ✅ |
| `node --test console/backend/test/*.test.mjs`（CI 同款 glob） | 全量后端单测/合同（含 legacy 契约域） | 259 pass / 0 fail / 10 skipped ✅ |
| `console/backend/test/mu-job-consumer.integration.mjs` | GitHub legacy 契约锁（C8h/C8i 原样）+取消端点等 | 51/51 ✅ |
| `console/frontend/test/*.test.mjs` | 前端全量（GitHub 页面回归） | 152/152 ✅ |
| `npx vite build` | 前端生产构建 | ✅（13.5s） |
| `bash scripts/secret-scan.sh --path .` | 秘密扫描 | PASS 0 命中 ✅ |

## 二、实现面（与实施设计的偏差记录）

1. **completeness 三态落点**：GiteeAdapter 返回完整三态；run 消费链 D5 门控在两处 dry-run 入口（消费链内联+审批后 buildFixDepsForRepo——declared 不可得即拒绝装配）。
2. **reviewDiff 零改动**：per-file patch 拼接为带 `diff --git` 文件头的 unified diff 全文——规则链复用；缺失 patch 文件不掺入 diff（completeness 单独表达）。
3. **leader 保护门**：`protection.provided===false` 显式分支（本接入未提供→跳过保护门、裁定只看 findings、rationale 记 not_provided_in_this_release）；GitHub legacy `configured` 语义原样。
4. **合并资格**：Gitee PR 快照 branch_protection_status='unknown'——按既有 fail-closed 呈现"未知（≠未受保护）"，详情页补「本接入未提供 ≠ 平台无保护」说明。
5. **错误分类**：GiteeProviderError（401→gitee_auth_failed/403→gitee_forbidden/404→gitee_not_found/429+5xx/网络→transient）；attempt FAILED→诚实 BLOCKED，不永久 queued。
6. **revoke 语义**：撤销不可逆（probe 不复活）——恢复=重新登记（upsert 清 revoked_at）+probe，集成测试实证。

## 三、试点前置（真实平台验收清单——本轮未做，不冒充）

以下为「代码就绪、待真实凭据」项；完成前 Gitee 不得宣称生产接入：

1. 试点仓库+私人令牌（user_info+projects+pull_requests；不勾 hook）——`r3work/forge-m0/pilot-setup-guide.md`。
2. 部署侧 env：MU_GITEE_PAT / MU_GITEE_WEBHOOK_SECRET / MU_GITEE_WEBHOOK_MODE。
3. 真实 webhook 采集：签名实际携带方式（header vs payload 顶层）、action 全枚举（reopen 存在性）、delivery 隐藏标识——回填核验记录 §5/§9 后收敛验真单源。
4. probeConnection 通过（授权已验证的唯一口径）+私有仓库真实读取。
5. 真实 PR 审查端到端（对照 rc.21 GitHub 验收口径：确定性链+幂等+审批门+dry-run 零写入）。
6. 限流被动观察（禁压测）。

## 四、恢复/降级语义（rc.22 生产镜像实测，scripts/dev/downgrade-drill-gitee.sh）

- **应用降级（rc.22 镜像实测=DOWNGRADE-VERIFIED）**：生产同源镜像（digest 35b453f3）连接 v25 隔离库：启动零 schema 兼容错误；GitHub legacy 数据（installation/binding/PR）完好可读；**旧 consumer 对 Gitee 任务的处理=明确拒绝**（`rejected|installation_id_missing`——按旧契约诚实失败，非误处理、非挂死、非崩溃）。降级窗口内 Gitee 功能不可用但无害；降级前建议先停用 Gitee 入队（任务停用/隔离=应用层决策，避免任务被旧 consumer 拒绝后需重投）。
- **schema 恢复（条件分列）**：DROP 两新表+两列技术上可行（无旧镜像依赖，实测共存无损）；约束是**数据取舍**——Gitee 连接登记行可弃（可重建）；Gitee 事件/run/审计历史若需保留则不得 DROP。恢复=显式决策+范围留痕，不宣称无条件前滚。
- **连接撤销恢复**：重登记（upsert 清 revoked_at）→probe→valid（T11-pre 实证）。

## 五、审查纠偏轮修复记录（2026-10-10 第二轮）

1. **验真算法修正（重要缺陷）**：v1 待签消息漏 secret 段（`HMAC(ts+LF)` vs 官方 `HMAC(ts+LF+secret, key=secret)`）——**独立向量**（python hmac/urllib 独立计算，非实现公式复刻）证实不一致后按官方原文修正；旧公式输出固化为防退化断言。
2. **验真来源/编码显式配置**：signSource（header|body）/signEncoding（url_b64|b64）由部署 env 单选，不自动回退/混合；另一来源携带不一致完整对→sign_source_conflict 拒绝。官方携带方式未实测的形态保持未启用。
3. **测试自洽暴露**：原集成测试的签名生成器同样沿用旧公式——被修正后的实现拒绝（signature_mismatch），防退化设计价值实证；测试侧签名生成器同步修正。
4. **上下文 v2**：仓库 id 归属核验（改名后同名新仓库→gitee_repo_moved）；head 双读（分页期间推进→gitee_head_moved）；declared 分页自证（末页不满页=全量已取得，**手动入口不再依赖 webhook 声明**）；local_limit 接真值（diff>1MiB/files>300/单文件 patch>512KiB）；空白路径 unparsed_path 防护（parseDiff 安全）。
5. **修复链端到端实证**：P0→审批门→维护者逐票真实批准（decide_review 门，不自动审批）→fxv 真子进程 Fixer dry-run（本地 fixture 与 stub head 同源）→Verifier PASS→run COMPLETED；全程远端零写调用、上下文读取全走 Gitee 适配器（fetchContextFn v2）。
6. **CI 修复**：超时测试 stub 的 pending Promise 在 CI 时序下挂起 event loop（连锁 cancel 7 用例）——abort 同步兜底修复。
7. **审批票面范围声明**：v25 冻结列（context_completeness/context_source）随审批列表 API 透出，详情页 partial/unknown 时展示范围声明（缺失=不显示，不伪造）。

## 六、发布前迁移演练（2026-10-10 第三轮，scripts/dev/migration-drill-v23-v25.mjs）

rc.22 基线（git tag 87f292e 的 schema v1-v23）bootstrap → 旧数据种子（GitHub 绑定/PR/历史 run/P0 finding/审批票/legacy 任务）→ 当前分支增量迁移（v24/v25）→ 12/12 PASS：

- 增量迁移不重放 v1-v23（schema_migrations 恰 25 行）；重跑幂等
- GitHub 绑定/PR 行/历史审批票/任务 payload 全部完好（外键级联无损）
- 历史 run 的 v25 冻结列=NULL（"未记录"不伪造）
- repository.forge_instance_id 回填 github-com（现读路径不引用）
- 新代码 legacy GitHub 服务链解析照常（resolveServiceContext 实测）

**生产切换前置门（第五节）迁移演练=通过。** 剩余前置=真实 Gitee 试点（凭据/仓库/webhook 授权——维护者人工动作，见 §三）。

## 七、试点启动指引（凭据就位后执行序列）

1. 维护者按 pilot-setup-guide 完成仓库+令牌+env（凭据只入 `D:\goai\secrets\gitee-pilot.env`）。
2. 部署隔离试点服务：rc.23 镜像+独立数据库+`MU_GITEE_PAT/_WEBHOOK_SECRET/_WEBHOOK_MODE=signature`（来源/编码 env 显式配置，不自动回退）；webhook 回调=试点服务的 `/api/mu/gitee/webhook`（HTTPS）。
3. 试点验收序列（验收记录 §三 1-6 逐项）→ 全过后按发布序列合入/发布/生产。

## 八、真实 Gitee 平台验收（2026-10-10 试点执行，生产同机隔离环境）

**全部通过（零 stub）**：probe（header 认证定案）/私有读取+绑定（稳定 id）/手动链 PR-1 clean COMPLETED（completeness complete 自证）/P0 审批门（真实 diff R-SECRET:P0→WAITING）/**agentteams 外部修复链 PASS→COMPLETED**（真实私有仓库 clone，digest ae91a373…）/撤销→重登记/泄漏检查全零（容器日志/audit/job/fix_attempt）。

**Webhook 真实投递（公网入口打通后）**：真实投递→验真→delivery_ref（body sha256 规则）→消费 done→新 run（v10）→P0 门→审批→agentteams 修复 DRY_RUN→PASS→COMPLETED。安全序列：错误签名→password_mismatch 401、缺签名→token_missing 401、未订阅事件→ignored 零入队。

**实测定案（修正文档级认知）**：
1. **验真语义**：当前 Gitee 实现 `X-Gitee-Token`=配置密钥明文（密码语义传输）；官方文档的 HMAC 签名算法在该配置下未使用（body sign 空字段）。配置收敛 `MU_GITEE_WEBHOOK_MODE=password`（显式配置，按实测收敛非降级）。文档级"签名不覆盖 body"论述保留（若未来启用真 HMAC 再评估）。
2. **事件订阅字段名**：`merge_requests_events`（PATCH `pull_request_events` 被静默忽略——v5 API 字段名与 GitHub 惯例不同）。
3. **事件行为**：源分支 push 与标题 PATCH **均触发** merge_request_hooks `action=update`（附 `action_desc:"source_branch_changed"` 等细分子类型）；创建 hook 时发一次 test 投递。
4. **delivery 标识**：无独立投递 ID 头/字段（ADR §2.4 生成规则启用，实测工作）。
5. **payload 红线实证**：投递 body 顶层含明文 `password` 字段——不持久化 body 的入口纪律为必需（现实现满足：delivery 行只存 delivery_ref/event）。
6. token 经 `Authorization: Bearer` header 可用（不进 URL）。

**公网入口（服务器变更留痕）**：frpc.toml 新增 `mergepilot-gitee-pilot` tcp 隧道（→本机 28600）；远程 nginx `mergepilot-webhook.conf` 加精确路径 `location = /api/mu/gitee/webhook` → 127.0.0.1:28600（备份 .bak-*；reload 零中断）；根路径与其余管理面不开放（实测 404）。**回滚**=删 location+reload（恢复原 conf.bak）+删 frpc 隧道段。

**维护者操作与测试身份的区分**：登录/probe/绑定/审查触发/投递观测=测试身份（fixture，隔离试点库）；审批动作=pilot-maintainer（测试身份的 maintainer 角色，走真实 decide_review 端点）——生产启用后须由真实维护者账号执行首次审批。
