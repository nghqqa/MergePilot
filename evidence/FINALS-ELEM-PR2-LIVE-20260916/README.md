# FINALS-ELEM-PR2-LIVE-20260916 — 真实 AgentTeams PR #2 案例（elemiso 隔离栈,CoPaw Leader 编排）

> 结论:**REAL_EXECUTED_AGENTTEAMS_COPAW — 全链闭环,首次通过(VERIFIED PASS)**。
> 本目录为本次真实运行的唯一权威证据;不含任何密码、token、API key。
> run_id:`run-elem-fastapi-pr2-20260916-01` · project:`elemiso-pr2-gate` · team:`elemiso-team`

## 一、案例定义

- 仓库:https://github.com/nghqqa/fastapi-boilerplate-demo(公开仓)
- PR:**#2**(branch `demo/high-risk-human-gate`)head SHA `1dedf5e1992c950557064d8f4fb9039d1523deb3`(运行前后经 GitHub API 双向核验一致,零 GitHub 写入)
- 主题:CWE-22 路径穿越/任意文件读取(`demo_high_risk.py::demo_download`)
- 流程:Reviewer 独立审查 → **人工安全门(操作员真实批准)** → Fixer 最小修复(本地 patch)→ Verifier 独立干净工作区验证 → Leader 按 TaskResult 验收
- 边界:PR #2 全程保持 OPEN;未 merge/push/close/reopen/comment;未修改仓库任何测试

## 二、运行时与资源(全部复用 P14 已验证镜像)

| 组件 | 容器 | 镜像(本地核验 ID) | 角色 |
|---|---|---|---|
| 嵌入控制器 | elemiso-ctrl | `agentteams/agentteams-embedded:223ddc2` = `ae47995d209f` | Matrix/MinIO/Controller/Higress 网关+控制台 |
| 平台 Manager | (已停,CR 保留) | `agentteams/manager:223ddc2` = `cafca0c1dc16` | openclaw;onboarding 完成后因失控 heartbeat 被停(见 AUDIT §3.1) |
| CoPaw Leader | elemiso-worker-leader(已停) | `agentteams/copaw-worker:223ddc2-build1` = `cdc8f8a4ab8d` | **业务编排权威**:projectflow/taskflow/delegate_task/check_task |
| Reviewer/Fixer/Verifier | elemiso-worker-{reviewer,fixer,verifier}(已停) | 同上 copaw-worker | 执行角色:ack_task→作业→submit_task(TaskResult) |
| Element Web | elemiso-element-web | `vectorim/element-web:latest` = `7050130b263b`(与 P14 容器同镜像) | 人可见交接窗口 http://127.0.0.1:18088 |
| 端口代理 | elemiso-proxy | `python:3.11-slim` = `1042b61448fe` | 18167→Matrix、18001→Higress 控制台、18090→Controller API |

- 状态权威:CoPaw Leader 的文件存储 `shared/projects/elemiso-pr2-gate/`(meta.json+plan.md)+ 任务 `shared/tasks/<tid>/{meta,spec,result}.md`,经 MinIO `teams/elemiso-team/shared/` 同步;控制器侧仅 CR/容器生命周期管理。
- Matrix:`elemiso-matrix:6167`(Tuwunel)。房间:团队房 `!RErK7WVs9iUeaszwho:elemiso-matrix:6167`(全部交接可见);admin↔Leader DM `!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167`(kickoff、人工门、最终报告)。
- LLM:deepseek-chat,经 Higress 路由 `default-ai-route`(consumer:key-auth,allowedConsumers=manager/worker-*)→ api.deepseek.com。运行模型与 skill 指令版本:AgentTeams v1.2.3(commit 223ddc2 镜像),copaw-worker 223ddc2-build1 内置模板。

## 三、关键事件链(容器内时钟;宿主时钟换算见 AUDIT §2)

| 阶段 | event_id | 摘要 |
|---|---|---|
| kickoff(首条,缺 mentions 未派发) | `$A0cmaWxYsthouxCcprsFa5z7JKrRM86kylsTNzxWVpA` | 操作员→Leader DM |
| kickoff(修正版,已消费) | `$LzIUwjIZSEFeD9ZKxVZSdcgWiSHAT1KvbGii96KiHrs` | 含 SPEC A/B/C 全文 |
| 委派 pr2-review-1 | `$lK5NAkgCQwxnNIoTahtvwFSQW4J_WrNqx5RyM24wNBI` | Leader taskflow(delegate_task),meta event_id 一致 |
| Reviewer 提交 | team room(TASK_COMPLETED 行) | FINDING_CONFIRMED / HIGH / CWE-22 / HUMAN_VERIFICATION_REQUIRED:YES |
| Leader 违规委派 fix-1(未过门) | 见 AUDIT §3.2 | 已由操作员作废并回滚 |
| 人工门批准(操作员真实决策) | DM `$5Bj24K0DHVkjc2BD2_8A9OR7Mh-U4d-PS_fw4WJ9sOM` / team `$jMI5NE6QkHHZNgqYuo6L5M_948ETdFgGjkxbq4s2b90` | 批准记录 `human-gate-approval.md` 落盘项目存储 |
| 重新委派 pr2-fix-1(过门后) | `$uyClhW_xZMMmxZx49V_e5buvoI3dHYTPcve-RnWIoV0` | FIX_APPLIED + SELF_CHECK_PASSED,patch sha256 `674356fc…16081` |
| Verifier 独立验证 | team room | **VERDICT=VERIFIED(PASS)**,sha256 独立复现一致,修复前 3 组越权 200→修复后全 400,合法访问 200,缺失 404,冻结测试预期反转(2 failed)如实记录 |
| Leader 最终报告 | DM `$91jWHmFauP7vmlr8lYKG78wh2K2KC92eRDRr-CQwmYo` | 项目 completed,三任务全验收 |

## 四、结果

| 任务 | 执行者 | 结果 | 产物 |
|---|---|---|---|
| pr2-review-1 | @reviewer (copaw) | SUCCESS,FINDING_CONFIRMED/HIGH/CWE-22 | reviewer-result.md、reviewer-pr2-review-repro.py |
| pr2-fix-1 | @fixer (copaw) | SUCCESS,FIX_APPLIED+SELF_CHECK_PASSED | fixer-attempt-1.diff(+13/−7,realpath+commonpath 包含性校验,400/404/200) |
| pr2-verify-1 | @verifier (copaw) | SUCCESS,**VERIFIED (PASS)**,首次通过 | verifier-workspace/{verification.md,verify_probe.py,attempt-1.diff} |

- 返工:未发生(自然首次 PASS,如实记录;不存在人为制造返工)。
- GitHub 合规:PR #2 open/未合并/head 不变;无新分支、无评论、无 push。

## 五、文件清单

| 文件 | 内容 |
|---|---|
| AUDIT.md | 时间线、异常(时钟漂移/Leader 跳门/Fixer 自启/工具护栏)、预算与用量、合规声明 |
| human-gate-approval.md | 本次人工门真实批准记录(范围+禁令+门前提 A) |
| team-room-messages.json / leader-dm-messages.json | Matrix 全量事件导出(233/112 条,含全部 event_id) |
| reviewer-*/fixer-*/verifier-* | 三角色 TaskResult 与工作区工件(sha256 见 fixer-meta-sha.txt/verifier-meta.txt) |
| higress-gateway-log-final.log | 网关访问+ai_log 全量日志(用量权威来源) |
| PR-METADATA.md | 下发给各角色的任务元数据(克隆命令/SHA/冻结约束) |
