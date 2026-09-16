# AUDIT — FINALS-ELEM-PR2-LIVE-20260916

如实记录。本文件不含任何凭据值。

## 1. 预算口径与实测用量

本轮提示词给定预算:deepseek-chat;全调用者合计 300,00000 token(原文如此,按字面取 300 万–3000 万区间;实际按费用硬约束执行);≤6000 次模型请求;费用 ≤¥10;≤120 分钟(自有效模型出口启用)。

**实测**(权威来源:`higress-gateway-log-final.log` 的 ai_log,含全部调用者):

| 口径 | 请求数 | 输入 token | 其中缓存命中 | 输出 token |
|---|---|---|---|---|
| 栈生命周期合计 | 339 | 11,312,747 | 10,880,000 (96.2%) | 63,524 |
| 其中 kickoff 之后(本次案例窗口) | 243 | 9,163,557 | — | 44,056 |
| 参考基线(kickoff 前,含昨日事故 3 次+平台 Manager heartbeat 68 次) | 71 | 1,700,149 | 1,594,112 | 16,763 |

- 费用估算(DeepSeek 公开价:缓存命中输入 ≈¥0.2–0.5/1M、未命中 ≈¥2/1M、输出 ≈¥3/1M):未命中输入 0.43M ≈ ¥0.87,命中输入 10.88M ≈ ¥2.2–5.4,输出 0.064M ≈ ¥0.2,合计 **≈¥3.3–6.5,未超 ¥10**。账单以供应商为准。
- 无法观测部分:网关 proxyNextUpstream(attempts=3)的内部上游重试不会逐条进访问日志;在途流断连后的供应商侧部分计费。
- 用量偏高的两个已定性原因(见 §3):① openclaw 平台 Manager 的 heartbeat(运行约 70 分钟、68 次调用、~1.65M 输入)在被识别后立即停止;② Fixer 未被委派时的自启会话在 2 分钟内产生 69 次调用后被立即叫停。正常案例本体(三角色+Leader 编排)调用量与 P14 历史量级一致。
- 达限动作从未触发;墙钟实际:preflight(容器钟 17:14)→ 停止计费调用者(宿主钟约 08:5x+08),真实耗时约 100 分钟(含跨会话中断与异常处置),未超 120 分钟。

## 2. 时钟漂移(如实声明)

运行中 Docker VM 时钟发生一次跳变:kickoff 发送时容器钟(=Matrix origin_server_ts、网关日志时间)落后宿主约 6 小时 21 分,其后自动同步。因此本目录内 Matrix event 的 `origin_server_ts` 与网关日志时间戳在跳变前的事件上比宿主墙钟早约 6.4 小时;事件**先后顺序**在容器钟内保持单调。宿主换算:kickoff 修正版实际发送 ≈宿主 2026-09-16 01:1x(+08) 前后,人工门批准 ≈01:3x(+08),全链收尾 ≈01:5x(+08)。凡需与外部系统对时的声明以宿主钟为准。

## 3. 异常与处置(全部如实记录,均不影响最终证据的有效性)

### 3.1 平台 Manager 失控 heartbeat(接手前已存在)
接手时 elemiso 栈已在运行(上一会话启动)。网关日志显示 openclaw 平台 Manager(非业务编排角色)存在周期性 HEARTBEAT 自主调用(每拍 ~3.4 万输入,96% 缓存命中),累计 68 次。处置:先置 CR `state=Stopped` 再移除容器(防 reconcile 拉起);此后零该类调用。**架构结论:业务编排不使用平台 Manager**(P14 已验证 CoPaw Leader 编排),平台 Manager 的 onboarding/heartbeat 是独立预算风险源,默认停用。

### 3.2 Leader 跳过人工门(协议违规,已作废)
Leader 在收到 HIGH 结论后未等人工批准即委派 pr2-fix-1。操作员立即:① 作废委派(@leader/@fixer 指令,event `$-Fp2BXToECtXUJrBpVt2MwliNnNhAp9ajgEdFKOiGXY`/`$JN6ZIBlUQ5Ay5FJdmRjb0GAt3nwixrmeAD8pqMH-i9w`);② 以操作员身份回滚 plan.md(`[~]`→`[ ]`,双树+MinIO 团队前缀+fixer 本地副本四处一致);③ 向用户展示真实 findings 后取得**本次真实批准**;④ Leader 随后正式重派(event `$uyClhW_xZMMmxZx49V_e5buvoI3dHYTPcve-RnWIoV0`)。被作废委派产生的任何工作不计入案例;**本次门批准不复用任何历史批准**。

### 3.3 Fixer 自启(未委派先开工,已叫停)
Fixer 因团队房间"上下文回放"机制收到 review 任务上下文后自行开工(2 分钟 69 次调用,产出未验收的中间修复)。操作员 @fixer 停工指令(event `$sOmHHoIoeCl4prVmv2i_7tLcwHV8AG6zWHgDlUU2KeU`)后其回合终止(工具护栏拒绝其后续命令);其自启工作被明确声明"不被接受、不入证据"。正式 fix-1 在门批准后由 Leader 委派执行。
> 附注:该现象源于共享房间拓扑+上下文回放机制(与 P14 同构),说明"人工门=编排拓扑+权限"的边界需要操作员主动执行;已如实写入 AUDIT,未粉饰为全自动门。

### 3.4 工具护栏(Tool Guard)交互
- Reviewer 的 clone 命令因含 `rm -rf` 被 `TOOL_CMD_DANGEROUS_RM` 拦截(HIGH);操作员 `/approve` 批准的事件因时钟跳变被护栏按"超时 23492s"自动拒绝;Reviewer 改用无 rm 的 clone 自行恢复,clone 结果 SHA 核验一致。
- copaw worker 的 matrix consumer 空闲 600s 自动清理(上游已知行为):两次导致委派/审批消息滞留。一次经"带 @mention 的消息"恢复;一次经容器重启恢复;Fixer 正式委派消费经重启+操作员提及消息恢复。**委派/审批的最终执行均由对应角色 agent 自主完成,操作员消息仅用于唤醒派发,不代替任何角色执行任务**。

### 3.5 kickoff 首条未派发
首条 kickoff 正文未以受派人 MXID 开头且无 `m.mentions`,copaw 桥不派发(P14 协议:正文首词 MXID)。补发修正版后正常。两条消息均留存于 DM 导出,作为协议依据。

## 4. 合规声明

- **GitHub 零写入**:PR #2 全程 open/未合并,head SHA 运行前后一致(`1dedf5e…`);无新分支/评论/push;Fixer/Verifier/Reviewer 均以公开 clone 只读+本地作业,交付为本地 patch。
- **冻结测试**:仓库测试文件未被任何角色修改;Verifier 明确区分"漏洞复现测试(修复后 2 failed=断言反转,预期)"与"安全验收探针(全过)",验收同时覆盖越权拒绝(3 组 400)、合法访问(200)、缺失(404)。
- **无伪造**:返工未发生即如实记"未发生";不宣称"已合并";人工门位置保持在修复前;旧 P14 证据未改名复用(本目录全部为本次新证据)。
- **凭据**:DeepSeek key 仅经容器内文件注入与 preflight 读取(用后即删),未回显、未入任何日志/证据/截图;栈内 consumer/matrix/MinIO 凭据均为控制器生成的栈内凭据。

## 5. 资源终态

- 已停止(计费调用者零残留):elemiso-worker-{leader,reviewer,fixer,verifier}(CR state=Stopped,容器由控制器移除,可随时恢复);elemiso-manager(更早已停)。
- 保留运行(零模型调用):elemiso-ctrl(Matrix/MinIO/Controller/网关,房间与全部状态完好)、elemiso-element-web(http://127.0.0.1:18088,用户可登录查看全部交接)、elemiso-proxy(18001 控制台/18090 API/18167 Matrix)。
- 卷/网络全部保留;删除任何残留属独立待确认操作。
